import pytest

from pymodaq_plugins_thermospectro.buffers import TimeSeries


class TestTimeSeries:
    def test_append(self):
        series = TimeSeries(2)
        series.append(0., [1., 2.])
        series.append(1., [3., 4.])

        assert len(series) == 2
        assert series.t.tolist() == [0., 1.]
        assert series.column(0).tolist() == [1., 3.]
        assert series.values.tolist() == [[1., 2.], [3., 4.]]

    def test_it_grows(self):
        series = TimeSeries(3, capacity=2)
        for i in range(1000):
            series.append(float(i), [i, 2 * i, -i])

        assert len(series) == 1000
        assert series.t[-1] == 999
        assert series.column(1)[500] == 1000
        assert series.column(2)[7] == -7

    def test_the_number_of_values_is_checked(self):
        with pytest.raises(ValueError, match='3 values'):
            TimeSeries(2).append(0., [1., 2., 3.])

    def test_no_column(self):
        series = TimeSeries(0)
        series.append(1., [])

        assert len(series) == 1 and series.values.shape == (1, 0)

    def test_last_rows(self):
        series = TimeSeries(1)
        for i in range(11):
            series.append(float(i), [i])

        assert series.t[series.last_rows(3.)].tolist() == [7., 8., 9., 10.]
        assert series.t[series.last_rows(0.)].tolist() == list(range(11))
        assert series.t[series.last_rows(100.)].tolist() == list(range(11))
        assert TimeSeries(1).last_rows(5.) == slice(0, 0)

    def test_clear(self):
        series = TimeSeries(1)
        series.append(0., [1.])
        series.clear()

        assert len(series) == 0
        assert series.t.size == 0
