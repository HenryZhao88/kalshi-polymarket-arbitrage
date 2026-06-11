"""Auth signers and WS message handling tests."""

import base64
from decimal import Decimal

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from arb_scanner.app.clients.kalshi_rest import KalshiSigner
from arb_scanner.app.clients.kalshi_ws import KalshiBookTracker, SequenceGapError
from arb_scanner.app.clients.polymarket_auth import (
    L2Credentials,
    l1_sign_order,
    l2_headers,
    l2_signature,
)
from arb_scanner.app.clients.polymarket_ws import (
    BookEvent,
    LastTradeEvent,
    NewMarketEvent,
    PriceChangeEvent,
    parse_event,
)

D = Decimal


@pytest.fixture(scope="module")
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class TestKalshiSigner:
    def test_headers_present_and_signature_verifies(self, rsa_key: rsa.RSAPrivateKey) -> None:
        pem = rsa_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        signer = KalshiSigner("key-id", pem)
        headers = signer.headers("get", "/trade-api/v2/portfolio", timestamp_ms=1700000000000)
        assert headers["KALSHI-ACCESS-KEY"] == "key-id"
        assert headers["KALSHI-ACCESS-TIMESTAMP"] == "1700000000000"
        # signature must verify over timestamp + METHOD + path
        rsa_key.public_key().verify(
            base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"]),
            b"1700000000000GET/trade-api/v2/portfolio",
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )


class TestPolymarketL2:
    SECRET = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode()

    def test_signature_is_deterministic_and_urlsafe(self) -> None:
        sig1 = l2_signature(self.SECRET, "1700000000", "GET", "/orders", "")
        sig2 = l2_signature(self.SECRET, "1700000000", "get", "/orders")
        assert sig1 == sig2
        base64.urlsafe_b64decode(sig1)  # decodable

    def test_headers_complete(self) -> None:
        creds = L2Credentials(
            address="0xabc", api_key="k", api_secret=self.SECRET, api_passphrase="p"
        )
        headers = l2_headers(creds, "POST", "/order", body="{}", timestamp=1700000000)
        assert set(headers) == {
            "POLY_ADDRESS",
            "POLY_SIGNATURE",
            "POLY_TIMESTAMP",
            "POLY_API_KEY",
            "POLY_PASSPHRASE",
        }

    def test_l1_fails_closed(self) -> None:
        with pytest.raises(NotImplementedError, match="disabled by design"):
            l1_sign_order()


SNAPSHOT = {
    "type": "orderbook_snapshot",
    "sid": 2,
    "seq": 10,
    "msg": {
        "market_ticker": "FED-23DEC-T3.00",
        "market_id": "uuid",
        "yes_dollars_fp": [["0.0800", "300.00"]],
        "no_dollars_fp": [["0.5400", "20.00"]],
    },
}


class TestKalshiBookTracker:
    def test_snapshot_then_delta(self) -> None:
        tracker = KalshiBookTracker()
        tracker.handle(SNAPSHOT)
        book = tracker.handle(
            {
                "type": "orderbook_delta",
                "sid": 2,
                "seq": 11,
                "msg": {
                    "market_ticker": "FED-23DEC-T3.00",
                    "market_id": "uuid",
                    "price_dollars": "0.0800",
                    "delta_fp": "-300.00",
                    "side": "yes",
                },
            }
        )
        assert book is not None
        assert book.yes_bids == ()

    def test_sequence_gap_raises(self) -> None:
        tracker = KalshiBookTracker()
        tracker.handle(SNAPSHOT)
        with pytest.raises(SequenceGapError):
            tracker.handle(
                {
                    "type": "orderbook_delta",
                    "sid": 2,
                    "seq": 13,  # gap: 11 missing
                    "msg": {
                        "market_ticker": "FED-23DEC-T3.00",
                        "market_id": "uuid",
                        "price_dollars": "0.08",
                        "delta_fp": "1.00",
                        "side": "yes",
                    },
                }
            )

    def test_delta_before_snapshot_raises(self) -> None:
        tracker = KalshiBookTracker()
        with pytest.raises(SequenceGapError, match="before any snapshot"):
            tracker.handle(
                {
                    "type": "orderbook_delta",
                    "sid": 2,
                    "seq": 1,
                    "msg": {
                        "market_ticker": "X",
                        "market_id": "uuid",
                        "price_dollars": "0.08",
                        "delta_fp": "1.00",
                        "side": "yes",
                    },
                }
            )

    def test_unrelated_messages_ignored(self) -> None:
        assert KalshiBookTracker().handle({"type": "subscribed", "id": 1}) is None


class TestPolymarketEventParsing:
    def test_book_event(self) -> None:
        events = parse_event({"event_type": "book", "asset_id": "tok", "bids": [], "asks": []})
        assert isinstance(events[0], BookEvent)

    def test_price_change_fans_out(self) -> None:
        events = parse_event(
            {
                "event_type": "price_change",
                "market": "0xcond",
                "price_changes": [
                    {
                        "asset_id": "tok1",
                        "price": "0.5",
                        "size": "10",
                        "side": "BUY",
                        "best_bid": "0.49",
                        "best_ask": "0.51",
                    },
                    {"asset_id": "tok2", "price": "0.4", "size": "1", "side": "SELL"},
                ],
            }
        )
        assert len(events) == 2
        first = events[0]
        assert isinstance(first, PriceChangeEvent)
        assert first.best_bid == D("0.49")

    def test_last_trade_price(self) -> None:
        events = parse_event(
            {
                "event_type": "last_trade_price",
                "asset_id": "tok",
                "price": "0.62",
                "size": "100",
                "side": "BUY",
                "fee_rate_bps": "1000",
            }
        )
        trade = events[0]
        assert isinstance(trade, LastTradeEvent)
        assert trade.fee_rate_bps == D("1000")

    def test_new_market_carries_fee_schedule(self) -> None:
        events = parse_event(
            {
                "event_type": "new_market",
                "condition_id": "0xcond",
                "fee_schedule": {"exponent": 1, "rate": 0.03, "taker_only": True},
            }
        )
        market = events[0]
        assert isinstance(market, NewMarketEvent)
        assert market.fee_schedule is not None and market.fee_schedule["rate"] == 0.03

    def test_unknown_event_returns_empty(self) -> None:
        assert parse_event({"event_type": "mystery"}) == []
