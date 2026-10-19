"""Electric dark pixels of some Ocean Insight models, as reported by python-seabreeze (pyseabreeze/devices.py, the same
values as in libseabreeze) and the pixels of the spectrum that the plugin has to keep

Each model is: (name, number of pixels, dark pixels as (first, last + 1) ranges, (start, stop) of the pixels kept)
"""

MODELS = [
    # dark pixels at the beginning of the detector
    ('USB2000PLUS', 2048, [(6, 21)], (21, 2048)),
    ('USB2000', 2048, [(2, 24)], (24, 2048)),
    ('HR4000', 3840, [(2, 13)], (13, 3840)),
    ('SR2', 2110, [(0, 24)], (24, 2110)),
    # dark pixels at the beginning and at the end of the detector, with or without padding after them
    ('QEPRO', 1044, [(0, 4), (1040, 1044)], (4, 1040)),
    ('QE65000', 1280, [(0, 4), (1040, 1044)], (4, 1040)),
    ('MAYA2000PRO', 2304, [(0, 4), (2064, 2068)], (4, 2064)),
    ('MAYA2000', 2304, [(0, 8), (2072, 2080)], (8, 2072)),
    # dark pixels at the end of the detector only
    ('ST', 1516, [(1503, 1516)], (0, 1503)),
    # no dark pixel at all
    ('STS', 1024, [], (0, 1024)),
    ('HDX', 2068, [], (0, 2068)),
    ('FX', 2136, [], (0, 2136)),
]


def dark_pixel_indices(ranges):
    """The indices of the dark pixels, as returned by get_electric_dark_pixel_indices"""
    return [index for first, stop in ranges for index in range(first, stop)]
