from __future__ import absolute_import, division

import dask
import dask.array as da
import dask.dataframe as dd
from collections import OrderedDict
from dask.base import tokenize, compute
import numpy as np

from datashader.core import bypixel
from datashader.compatibility import apply
from datashader.compiler import compile_components
from datashader.glyphs import Glyph, LineAxis0
from datashader.utils import Dispatcher

__all__ = ()


@bypixel.pipeline.register(dd.DataFrame)
def dask_pipeline(df, schema, canvas, glyph, summary, cuda=False):
    dsk, name = glyph_dispatch(glyph, df, schema, canvas, summary, cuda=cuda)

    # Get user configured scheduler (if any), or fall back to default
    # scheduler for dask DataFrame
    scheduler = dask.base.get_scheduler() or df.__dask_scheduler__

    if isinstance(dsk, da.Array):
        return da.compute(dsk, scheduler=scheduler)[0]

    keys = df.__dask_keys__()
    optimize = df.__dask_optimize__
    graph = df.__dask_graph__()

    dsk.update(optimize(graph, keys))
    return scheduler(dsk, name)


def shape_bounds_st_and_axis(df, canvas, glyph):
    if not canvas.x_range or not canvas.y_range:
        x_extents, y_extents = glyph.compute_bounds_dask(df)
    else:
        x_extents, y_extents = None, None

    x_range = canvas.x_range or x_extents
    y_range = canvas.y_range or y_extents
    x_min, x_max, y_min, y_max = bounds = compute(*(x_range + y_range))
    x_range, y_range = (x_min, x_max), (y_min, y_max)

    width = canvas.plot_width
    height = canvas.plot_height

    x_st = canvas.x_axis.compute_scale_and_translate(x_range, width)
    y_st = canvas.y_axis.compute_scale_and_translate(y_range, height)
    st = x_st + y_st
    shape = (height, width)

    x_axis = canvas.x_axis.compute_index(x_st, width)
    y_axis = canvas.y_axis.compute_index(y_st, height)
    axis = OrderedDict([(glyph.x_label, x_axis), (glyph.y_label, y_axis)])

    return shape, bounds, st, axis


glyph_dispatch = Dispatcher()


@glyph_dispatch.register(Glyph)
def default(glyph, df, schema, canvas, summary, cuda=False):
    shape, bounds, st, axis = shape_bounds_st_and_axis(df, canvas, glyph)

    # Compile functions
    create, info, append, combine, finalize = \
        compile_components(summary, schema, glyph, cuda=cuda)
    x_mapper = canvas.x_axis.mapper
    y_mapper = canvas.y_axis.mapper
    extend = glyph._build_extend(x_mapper, y_mapper, info, append)

    def chunk(df, axis, keepdims):
        aggs = create(shape)
        extend(aggs, df, st, bounds)
        return aggs

    # Get the dataframe graph
    graph = df.__dask_graph__()
    # Get the topmost layer representing the creation of the dataframe
    df_layer = graph.layers[df._name]
    # Create a chunks tuple, a singleton for each dataframe chunk
    chunks = (tuple(1 for _ in range(len(df_layer))),)
    # Guess output dtype from combination of dataframe dtypes
    dtype = np.result_type(*df.dtypes)
    # Create a meta object
    meta = np.empty((0,), dtype=dtype)

    # Now create a dask array from the dataframe graph layer
    df_array = da.Array(graph, df._name, chunks, meta=meta)

    def wrapped_combine(x, axis, keepdims):
        """ wrap datashader combine in dask.array.reduction combine """
        if isinstance(x, list):
            # list of tuples of ndarrays
            # assert all(isinstance(item, tuple) and
            #            len(item) == 1 and
            #            isinstance(item[0], np.ndarray)
            #            for item in x)
            return combine(x)
        elif isinstance(x, tuple):
            # tuple with single ndarray
            # assert len(x) == 1 and isinstance(x[0], np.ndarray)
            return x
        else:
            raise TypeError("Unknown type %s in wrapped_combine" % type(x))

    local_ax = axis

    def wrapped_finalize(x, axis, keepdims):
        return finalize(wrapped_combine(x, axis, keepdims),
                        cuda=cuda, coords=local_ax,
                        dims=[glyph.y_label, glyph.x_label])

    R = da.reduction(df_array,
                     aggregate=wrapped_finalize,
                     chunk=chunk,
                     combine=wrapped_combine,
                     concatenate=False,
                     meta=meta,
                     dtype=meta.dtype)

    return R, R.name


@glyph_dispatch.register(LineAxis0)
def line(glyph, df, schema, canvas, summary, cuda=False):
    if cuda:
        from cudf import concat
    else:
        from pandas import concat

    shape, bounds, st, axis = shape_bounds_st_and_axis(df, canvas, glyph)

    # Compile functions
    create, info, append, combine, finalize = \
        compile_components(summary, schema, glyph, cuda=cuda)
    x_mapper = canvas.x_axis.mapper
    y_mapper = canvas.y_axis.mapper
    extend = glyph._build_extend(x_mapper, y_mapper, info, append)

    def chunk(df, df2=None):
        plot_start = True
        if df2 is not None:
            df = concat([df.iloc[-1:], df2])
            plot_start = False
        aggs = create(shape)
        extend(aggs, df, st, bounds, plot_start=plot_start)
        return aggs

    name = tokenize(df.__dask_tokenize__(), canvas, glyph, summary)
    old_name = df.__dask_tokenize__()
    dsk = {(name, 0): (chunk, (old_name, 0))}
    for i in range(1, df.npartitions):
        dsk[(name, i)] = (chunk, (old_name, i - 1), (old_name, i))
    keys2 = [(name, i) for i in range(df.npartitions)]
    dsk[name] = (apply, finalize, [(combine, keys2)],
                 dict(cuda=cuda, coords=axis, dims=[glyph.y_label, glyph.x_label]))
    return dsk, name
