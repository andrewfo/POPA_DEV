"""Tests for aisstream envelope parsing (pure — no database)."""
from app.ais.messages import AISPosition, AISStatic, parse_aisstream


def test_parse_position_report():
    env = {
        "MessageType": "PositionReport",
        "MetaData": {"MMSI": 367123456, "time_utc": "2024-01-19 10:30:00.123456789 +0000 UTC"},
        "Message": {
            "PositionReport": {
                "Latitude": 29.87,
                "Longitude": -93.93,
                "Sog": 0.1,
                "Cog": 145.0,
                "TrueHeading": 150,
                "NavigationalStatus": 5,
            }
        },
    }
    msg = parse_aisstream(env)
    assert isinstance(msg, AISPosition)
    assert msg.mmsi == 367123456
    assert msg.lat == 29.87 and msg.lon == -93.93
    assert msg.sog == 0.1 and msg.heading == 150.0
    assert msg.nav_status == 5
    assert msg.msg_ts is not None and msg.msg_ts.year == 2024


def test_position_sentinels_become_none():
    env = {
        "MessageType": "PositionReport",
        "MetaData": {"MMSI": 1},
        "Message": {
            "PositionReport": {
                "Latitude": 1.0, "Longitude": 2.0,
                "Sog": 102.3, "Cog": 360.0, "TrueHeading": 511,
            }
        },
    }
    msg = parse_aisstream(env)
    assert msg.sog is None and msg.cog is None and msg.heading is None


def test_parse_ship_static_data():
    env = {
        "MessageType": "ShipStaticData",
        "MetaData": {"MMSI": 367123456, "ShipName": "META NAME"},
        "Message": {
            "ShipStaticData": {
                "ImoNumber": 9123456,
                "Name": "EVER GIVEN@@@",
                "CallSign": "ABCD",
                "Type": 70,
                "Dimension": {"A": 200, "B": 100, "C": 20, "D": 12},
                "MaximumStaticDraught": 12.5,
                "Destination": "PORT ARTHUR",
            }
        },
    }
    msg = parse_aisstream(env)
    assert isinstance(msg, AISStatic)
    assert msg.imo == 9123456
    assert msg.name == "EVER GIVEN"  # '@' padding stripped
    assert msg.loa == 300.0  # A + B
    assert msg.beam == 32.0  # C + D
    assert msg.draft == 12.5
    assert msg.destination == "PORT ARTHUR"


def test_unknown_message_type_returns_none():
    assert parse_aisstream({"MessageType": "Other", "MetaData": {"MMSI": 1}}) is None


def test_missing_mmsi_returns_none():
    assert parse_aisstream({"MessageType": "PositionReport", "MetaData": {}}) is None
