"""Step 2 tests: book structure — sorted levels, FIFO within a level."""

import unittest

from lob.book import BookSide, OrderBook, PriceLevel
from lob.types import Order, Side


def buy(oid, price, qty):
    return Order(oid, Side.BUY, price=price, quantity=qty)


def sell(oid, price, qty):
    return Order(oid, Side.SELL, price=price, quantity=qty)


class TestPriceLevel(unittest.TestCase):
    def test_fifo_order_and_total_qty(self):
        level = PriceLevel(100)
        level.append(buy("a", 100, 10))
        level.append(buy("b", 100, 20))
        self.assertEqual([o.order_id for o in level.orders], ["a", "b"])
        self.assertEqual(level.total_qty, 30)

    def test_reduce(self):
        level = PriceLevel(100)
        level.append(buy("a", 100, 10))
        level.reduce(4)
        self.assertEqual(level.total_qty, 6)


class TestBookSide(unittest.TestCase):
    def test_best_bid_is_highest_price(self):
        bids = BookSide(Side.BUY)
        for oid, px in [("a", 99), ("b", 101), ("c", 100)]:
            bids.add(buy(oid, px, 10))
        self.assertEqual(bids.best_price(), 101)

    def test_best_ask_is_lowest_price(self):
        asks = BookSide(Side.SELL)
        for oid, px in [("a", 105), ("b", 102), ("c", 108)]:
            asks.add(sell(oid, px, 10))
        self.assertEqual(asks.best_price(), 102)

    def test_wrong_side_rejected(self):
        bids = BookSide(Side.BUY)
        with self.assertRaises(ValueError):
            bids.add(sell("a", 100, 10))

    def test_same_price_orders_share_a_level_fifo(self):
        bids = BookSide(Side.BUY)
        bids.add(buy("first", 100, 5))
        bids.add(buy("second", 100, 5))
        self.assertEqual(len(bids), 1)  # one level
        level = bids.best_level()
        self.assertEqual([o.order_id for o in level.orders], ["first", "second"])

    def test_levels_best_first_bids_descend(self):
        bids = BookSide(Side.BUY)
        for oid, px in [("a", 99), ("b", 101), ("c", 100)]:
            bids.add(buy(oid, px, 10))
        self.assertEqual([lv.price for lv in bids.levels_best_first()], [101, 100, 99])

    def test_levels_best_first_asks_ascend(self):
        asks = BookSide(Side.SELL)
        for oid, px in [("a", 105), ("b", 102), ("c", 108)]:
            asks.add(sell(oid, px, 10))
        self.assertEqual([lv.price for lv in asks.levels_best_first()], [102, 105, 108])

    def test_remove_best_level_promotes_next(self):
        bids = BookSide(Side.BUY)
        bids.add(buy("a", 101, 10))
        bids.add(buy("b", 100, 10))
        level = bids.best_level()
        level.orders.clear()
        level.reduce(10)
        bids.remove_level(101)
        self.assertEqual(bids.best_price(), 100)

    def test_remove_mid_book_level(self):
        asks = BookSide(Side.SELL)
        for oid, px in [("a", 102), ("b", 105), ("c", 108)]:
            asks.add(sell(oid, px, 10))
        mid = next(lv for lv in asks.levels_best_first() if lv.price == 105)
        mid.orders.clear()
        mid.reduce(10)
        asks.remove_level(105)
        self.assertEqual([lv.price for lv in asks.levels_best_first()], [102, 108])

    def test_empty_side(self):
        side = BookSide(Side.BUY)
        self.assertTrue(side.is_empty)
        self.assertIsNone(side.best_price())
        self.assertIsNone(side.best_level())


class TestOrderBook(unittest.TestCase):
    def test_best_bid_and_ask(self):
        book = OrderBook()
        book.bids.add(buy("b1", 100, 10))
        book.asks.add(sell("s1", 103, 10))
        self.assertEqual(book.best_bid(), 100)
        self.assertEqual(book.best_ask(), 103)

    def test_side_lookup(self):
        book = OrderBook()
        self.assertIs(book.side(Side.BUY), book.bids)
        self.assertIs(book.side(Side.SELL), book.asks)

    def test_empty_book(self):
        book = OrderBook()
        self.assertIsNone(book.best_bid())
        self.assertIsNone(book.best_ask())


if __name__ == "__main__":
    unittest.main()
