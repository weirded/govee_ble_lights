import array, base64, importlib.util, json, sys
D = sys.argv[1]
def load(name, f):
    spec = importlib.util.spec_from_file_location(name, f"{D}/{f}"); m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m; spec.loader.exec_module(m); return m
p = load("protocol", "protocol.py"); u = load("govee_utils", "govee_utils.py")
caps = """aa010100000000000000000000000000000000aa aa046400000000000000000000000000000000ca
aa05043308000000000000000000000000000090 aa06312e30332e3035000000000000000000009b
aaa5016400ff3f6400ff3f6400ff3f00000000aa aaa5026400ff3f6400ff3f6400ff3f00000000a9
aaa5036400ff3f6400ff3f00000000000000000c""".split()
s = p.LightState()
for c in caps: assert p.apply_status_frame(s, bytes.fromhex(c)) is not None, c
p.trim_segments(s, "H6072")
assert (s.power, s.brightness_raw, s.mode, s.scene_code, s.firmware) == (True, 100, 4, 0x0833, "1.03.05")
assert len(s.segments) == 8 and s.uniform_rgb() == (0, 255, 63)
assert p.apply_status_frame(s, bytes.fromhex("0013" + "00"*18)) is None
assert p.power(True).hex() == "3301010000000000000000000000000000000033"
assert p.power(False).hex() == "3301000000000000000000000000000000000032"
assert p.query(1).hex() == "aa010000000000000000000000000000000000ab"
assert p.query(0xa5, 1).hex() == "aaa501000000000000000000000000000000000e"
assert len(p.status_queries("H6072")) == 6
assert p.segment_mask([1, 8]) == 0x81 and p.all_segments_mask("H6072") == 0xFF
n = 0
for cat in json.load(open(f"{D}/jsons/H6072.json"))['data']['categories']:
    for sc in cat['scenes']:
        for le in sc['lightEffects']:
            d = base64.b64decode(le['scenceParam'])
            if not d: continue
            a = [bytes(x) for x in u.prepareMultiplePacketsData(0xa3, array.array('B', [2]), array.array('B', d))]
            assert a == p.multi_packet(0xa3, [2], d), len(d); n += 1
print("ok; multi_packet matches upstream on", n, "scene payloads")
print("CT 2700K:", p.color_temp_segments(2700, (255, 166, 87), 0xFF).hex())
# live-captured on 2026-11-23: CT mode readback and command acks
s2 = p.LightState()
p.apply_status_frame(s2, bytes.fromhex("aa0515000a8c000000000000000000000000003c"))
assert s2.mode == 0x15 and s2.kelvin == 2700
p.apply_status_frame(s2, bytes.fromhex("aa051500196400000000000000000000000000c7"))
assert s2.kelvin == 6500
p.apply_status_frame(s2, bytes.fromhex("aa051500000000000000000000000000000000ba"))
assert s2.kelvin is None
p.apply_status_frame(s2, bytes.fromhex("aa0504200000000000000000000000000000008b"))
assert s2.scene_code == 32 and s2.kelvin is None
assert p.parse_ack(bytes.fromhex("3305000000000000000000000000000000000036")) == (5, True)
assert p.parse_ack(bytes.fromhex("3305010000000000000000000000000000000037")) == (5, False)
assert p.color_temp_segments(2700, (255, 166, 87), 0xFF)[7:9] == bytes.fromhex("0a8c")
print("ok; CT readback + acks")
# H7013 (fw 1.00.23), captured 2026-11-23
s3 = p.LightState()
for c in ["aa0100", "aa042a", "aa05042c00ff"]:
    p.apply_status_frame(s3, p.build_frame(bytes.fromhex(c)))
assert (s3.power, s3.brightness_raw, s3.scene_code) == (False, 42, 44)
p.apply_status_frame(s3, p.build_frame(bytes.fromhex("aa050d00ff00")))
assert s3.uniform_rgb() == (0, 255, 0) and s3.kelvin is None
assert p.rgb_manual(0, 255, 0, p.MANUAL_COLOR_MODES["H7013"])[:6].hex() == "33050d00ff00"
assert p.status_queries("H7013") == [p.query(1), p.query(4), p.query(5)]
print("ok; H7013")
# H6072 CT range + app-captured tints (cromulus 5e74328)
assert p.ct_range("H6072") == (2200, 6500) and p.ct_range("H7013") == (2000, 9000)
assert p.ct_tint("H6072", 2200) == (0xff, 0x98, 0x29) and p.ct_tint("H6072", 6500) == (0xff, 0xf9, 0xfb)
assert p.ct_tint("H6072", 2749) == p.ct_tint("H6072", 2700) and p.ct_tint("H6072", 1500) == p.ct_tint("H6072", 2200)
assert p.ct_tint("H7013", 3000) is None
print("ok; CT tints")
# H6072 music mode (verified 2026-11-23: 33 05 0F accepted, read back verbatim)
assert p.music(3, 99).hex()[:18] == "33050f036301000000"
assert p.music(0, 99, (0, 0, 255)).hex()[:18] == "33050f00630000" + "00ff"[:4]
sm = p.LightState(); p.apply_status_frame(sm, p.build_frame(bytes.fromhex("aa050f0063010000ff")))
assert sm.music_mode == 0 and sm.scene_code is None
print("ok; music")
# H7061 (fw 1.00.05) captured 2026-11-23
s4 = p.LightState()
for c in ["aa0100", "aa0428", "aa051564ff00006400ff00", "aaa50164ff00006400ff00"]:
    p.apply_status_frame(s4, p.build_frame(bytes.fromhex(c)))
p.trim_segments(s4, "H7061")
assert s4.kelvin is None and len(s4.segments) == 2 and s4.segments[1].rgb == (255, 0, 0) and s4.segments[2].rgb == (0, 255, 0)
assert p.all_segments_mask("H7061") == 3 and len(p.status_queries("H7061")) == 4
print("ok; H7061")
