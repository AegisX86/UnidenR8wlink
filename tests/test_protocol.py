"""Parser tests against captured packets. No Uniden needed

`pip install pytest && pytest`.

Anything marked "captured" is a real packet off a real R8w. The rest is
synthetic, because I have never seen the real thing (a laser hit, a
populated POI database) and would rather have a test than not."""

from r8link import parse_alerts, parse_poi, parse_telemetry


def test_telemetry():
    t = parse_telemetry("12.1&0&W,0,193,C&0&12&D&D")
    assert (t.voltage, t.heading, t.speed, t.altitude) == (12.1, "W", 0, 193)
    assert t.gps_locked and t.poi is None and t.scan_count == 12


def test_telemetry_captured():
    # Captured, parked, engine off.
    t = parse_telemetry("12.2&0&S,0,2634,C&0&4&D&D")
    assert t.voltage == 12.2 and t.altitude == 2634 and t.gps_locked


def test_telemetry_with_poi():
    t = parse_telemetry("11.8&SPEEDCAM,500,35&N,45,312,C&0&5&D&D")
    assert t.poi.kind == "SPEEDCAM" and t.poi.speed_limit == 35
    assert t.speed == 45


def test_telemetry_survives_a_short_packet():
    assert parse_telemetry("12.1&0").scan_count is None


def test_no_alerts():
    assert parse_alerts("0&0&0&0") == []


def test_radar_alert():
    a = parse_alerts("1,00,KA,3,33,33.7850,R,1&0&0&0")[0]
    assert a.band == "KA" and a.strength == 3 and a.raw_signal == 33
    assert a.frequency_ghz == 33.785 and a.direction_name == "rear"
    assert not a.is_muted


def test_radar_alert_captured():
    # Captured. Some 35.2 GHz source in the house, probably a door sensor.
    a = parse_alerts("1,00,KA,1,40,35.2000,F,1&0&0&0")[0]
    assert a.band == "KA" and a.strength == 1
    assert a.frequency_ghz == 35.2 and a.direction_name == "front"
    assert a.description == "35.2 GHz" and not a.is_muted


def test_muted_alert_captured():
    # Same source, captured with mute pressed on the detector.
    a = parse_alerts("1,00,KA,1,31,35.2000,F,2&0&0&0")[0]
    assert a.is_muted and a.mute_status == "muted"


def test_two_at_once():
    alerts = parse_alerts("1,00,K,3,25,24.1500,F,1&1,00,KA,6,55,35.5000,R,1&0&0")
    assert [a.band for a in alerts] == ["K", "KA"]


def test_laser_uses_gun_id_not_frequency():
    a = parse_alerts("1,00,LASER,8,0,5,F,1&0&0&0")[0]
    assert a.laser_gun == "Kustom" and a.frequency_ghz is None


def test_unknown_laser_id_does_not_explode():
    assert "99" in parse_alerts("1,00,LASER,8,0,99,F,1&0&0&0")[0].laser_gun


def test_empty_poi_database_captured():
    # What an R8w with nothing stored actually returns: one type-0 byte.
    # The walk has to stop rather than spin, since type 0 has no length.
    assert parse_poi(bytes.fromhex("0000")) == []


def test_poi_stream():
    # Synthetic: a 35 mph speed camera followed by a red light camera.
    pois = parse_poi(bytes.fromhex(
        "0100423e7b4ac2f4b2bd004523" "0200423e7b4ac2f4b2bd0045"))
    assert [p.kind for p in pois] == ["speed camera", "red light camera"]
    assert pois[0].speed_limit == 35 and pois[1].speed_limit is None
    assert round(pois[0].latitude, 3) == 47.62
