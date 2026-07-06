"""A guided tour of the matching engine. Run:  python demo.py"""

from lob import MatchingEngine, Order, Side, TimeInForce

eng = MatchingEngine()

print("1) Build a book: three sellers, two buyers (none cross yet)")
eng.submit_limit(Order("alice", Side.SELL, price=10100, quantity=50))
eng.submit_limit(Order("bob",   Side.SELL, price=10100, quantity=30))
eng.submit_limit(Order("carol", Side.SELL, price=10200, quantity=40))
eng.submit_limit(Order("dave",  Side.BUY,  price=10000, quantity=60))
eng.submit_limit(Order("erin",  Side.BUY,  price=9900,  quantity=25))
print("   depth:", eng.depth(3))
print("   best bid/ask:", eng.best_bid(), "/", eng.best_ask())

print("\n2) A buyer crosses the spread: buy 60 @ 10150")
fills = eng.submit_limit(Order("frank", Side.BUY, price=10150, quantity=60))
for f in fills:
    print(f"   TRADE {f.quantity} @ {f.price}  (maker={f.maker_order_id})")
print("   -> alice filled first (same price as bob, but earlier = FIFO)")
print("   -> traded at 10100, not frank's 10150 (maker's price)")
print("   depth:", eng.depth(3))

print("\n3) Market order: sell 70 at any price")
fills = eng.submit_market(Order("gina", Side.SELL, price=None, quantity=70))
for f in fills:
    print(f"   TRADE {f.quantity} @ {f.price}  (maker={f.maker_order_id})")

print("\n4) Cancel and modify")
eng.submit_limit(Order("hank", Side.BUY, price=9950, quantity=100))
eng.modify("hank", new_qty=40)        # shrink in place: keeps queue spot
eng.cancel("erin")
print("   hank now:", eng.open_order("hank"))
print("   depth:", eng.depth(3))

print("\n5) IOC and FOK")
fills = eng.submit_limit(Order("ioc1", Side.BUY, price=10200, quantity=999),
                         tif=TimeInForce.IOC)
print(f"   IOC filled {sum(f.quantity for f in fills)}, rest cancelled "
      f"(best bid still {eng.best_bid()} — nothing rested)")
fills = eng.submit_limit(Order("fok1", Side.SELL, price=9900, quantity=99999),
                         tif=TimeInForce.FOK)
print(f"   FOK for 99,999: {len(fills)} fills — killed, book untouched")

print("\n6) Everything above was also recorded as an event stream:")
for e in eng.drain_events()[:8]:
    print("  ", e)
print("   ... (drain_events() returns them all, oldest first)")
