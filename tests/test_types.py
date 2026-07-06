"""Step 1 tests: core types and validation."""

import unittest

from lob.types import Side, Order, Fill


class TestSide(unittest.TestCase):
    def test_opposite(self):
        self.assertIs(Side.BUY.opposite, Side.SELL)
        self.assertIs(Side.SELL.opposite, Side.BUY)


class TestOrderValidation(unittest.TestCase):
    def test_valid_order(self):
        o = Order("o1", Side.BUY, price=10050, quantity=100)
        self.assertEqual(o.remaining, 100)
        self.assertFalse(o.is_filled)

    def test_empty_id_rejected(self):
        with self.assertRaises(ValueError):
            Order("", Side.BUY, price=100, quantity=10)

    def test_zero_and_negative_price_rejected(self):
        for bad in (0, -5):
            with self.assertRaises(ValueError):
                Order("o1", Side.BUY, price=bad, quantity=10)

    def test_zero_and_negative_quantity_rejected(self):
        for bad in (0, -1):
            with self.assertRaises(ValueError):
                Order("o1", Side.SELL, price=100, quantity=bad)

    def test_float_price_rejected(self):
        with self.assertRaises(TypeError):
            Order("o1", Side.BUY, price=100.5, quantity=10)

    def test_float_quantity_rejected(self):
        with self.assertRaises(TypeError):
            Order("o1", Side.BUY, price=100, quantity=10.0)

    def test_bool_price_rejected(self):
        # bool is an int subclass; make sure True doesn't sneak in as price 1
        with self.assertRaises(TypeError):
            Order("o1", Side.BUY, price=True, quantity=10)

    def test_non_side_rejected(self):
        with self.assertRaises(TypeError):
            Order("o1", "BUY", price=100, quantity=10)


class TestOrderFill(unittest.TestCase):
    def test_partial_then_full_fill(self):
        o = Order("o1", Side.BUY, price=100, quantity=10)
        o.fill(4)
        self.assertEqual(o.remaining, 6)
        self.assertEqual(o.quantity, 10)  # original size unchanged
        self.assertFalse(o.is_filled)
        o.fill(6)
        self.assertTrue(o.is_filled)

    def test_overfill_rejected(self):
        o = Order("o1", Side.BUY, price=100, quantity=10)
        with self.assertRaises(ValueError):
            o.fill(11)

    def test_nonpositive_fill_rejected(self):
        o = Order("o1", Side.BUY, price=100, quantity=10)
        for bad in (0, -3):
            with self.assertRaises(ValueError):
                o.fill(bad)


class TestTimePriority(unittest.TestCase):
    def test_seq_strictly_increasing(self):
        a = Order("a", Side.BUY, price=100, quantity=1)
        b = Order("b", Side.BUY, price=100, quantity=1)
        c = Order("c", Side.SELL, price=100, quantity=1)
        self.assertLess(a.seq, b.seq)
        self.assertLess(b.seq, c.seq)


class TestFill(unittest.TestCase):
    def test_valid_fill(self):
        f = Fill(maker_order_id="m", taker_order_id="t", price=100, quantity=5)
        self.assertEqual(f.price, 100)

    def test_fill_is_immutable(self):
        f = Fill("m", "t", price=100, quantity=5)
        with self.assertRaises(AttributeError):
            f.price = 200

    def test_invalid_fill_rejected(self):
        with self.assertRaises(ValueError):
            Fill("m", "t", price=0, quantity=5)
        with self.assertRaises(ValueError):
            Fill("m", "t", price=100, quantity=0)


if __name__ == "__main__":
    unittest.main()
