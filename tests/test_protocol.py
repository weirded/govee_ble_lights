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
