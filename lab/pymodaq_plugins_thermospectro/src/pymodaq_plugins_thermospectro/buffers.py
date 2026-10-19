"""In-memory history of what has been recorded, to plot it

Pure numpy, no Qt and no PyMoDAQ.
"""
from __future__ import annotations

from typing import Sequence

import numpy as np


class TimeSeries:
    """A table that grows: one time column and `n_columns` value columns

    Parameters
    ----------
    n_columns: int
        The number of value columns
    capacity: int
        The number of rows allocated at first, doubled whenever it is full
    """

    def __init__(self, n_columns: int, capacity: int = 1024):
        self._n_columns = int(n_columns)
        self._t = np.empty(max(int(capacity), 1))
        self._values = np.empty((self._t.size, self._n_columns))
        self._size = 0

    def __len__(self) -> int:
        return self._size

    @property
    def n_columns(self) -> int:
        return self._n_columns

    @property
    def t(self) -> np.ndarray:
        """The times, as a view: it is only valid until the next append"""
        return self._t[:self._size]

    @property
    def values(self) -> np.ndarray:
        """All the columns, shape (len, n_columns), as a view: it is only valid until the next append"""
        return self._values[:self._size]

    def column(self, index: int) -> np.ndarray:
        return self._values[:self._size, index]

    def append(self, t: float, values: Sequence[float]):
        if len(values) != self._n_columns:
            raise ValueError(f'{len(values)} values were given for {self._n_columns} columns')
        if self._size == self._t.size:
            self._t = np.concatenate([self._t, np.empty_like(self._t)])
            self._values = np.concatenate([self._values, np.empty_like(self._values)])
        self._t[self._size] = t
        self._values[self._size] = values
        self._size += 1

    def last_rows(self, span: float) -> slice:
        """The rows of the last `span` seconds (all of them if span <= 0), the times being increasing"""
        if span <= 0 or self._size == 0:
            return slice(0, self._size)
        return slice(int(np.searchsorted(self.t, self._t[self._size - 1] - span, side='left')), self._size)

    def clear(self):
        self._size = 0
