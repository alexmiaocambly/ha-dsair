"""分房间地暖温控器（设备类型 10/52）：用 DTA117D611 上抓到的真实报文验证解码和状态合并。"""

from pathlib import Path
import sys
import unittest
import select  # noqa: F401 — preload stdlib select to prevent clash with custom_components/ds_air/select.py


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components" / "ds_air"))

from ds_air_service.config import Config  # noqa: E402
from ds_air_service.ctrl_enum import EnumCmdType, EnumDevice  # noqa: E402
from ds_air_service.dao import FloorHeaterStatus, Room  # noqa: E402
from ds_air_service.decoder import (  # noqa: E402
    FloorHeaterNotifyResult,
    FloorHeaterQueryResult,
    decoder,
)
from ds_air_service.param import FloorHeaterQueryParam  # noqa: E402
from ds_air_service.service import Service  # noqa: E402

# 查询全部（子体 ff）的回包：5 台，全部关着，设定温度 24 / 24 / 21 / 21 / 21
QUERY_ALL = bytes.fromhex(
    "02a2000d000000140000000a3400000001030005"
    "01010630c9220000010201010601010701000802f0000c01000d010600"
    "02010630c9220000020201010601010701000802f0000c01000d010600"
    "03010630c9220000030201010601010701000802d2000c01000d010600"
    "04010630c9220000040201010601010701000802d2000c01000d010600"
    "0501064022d80000050201010601010701000802d2000c01000d010600"
    "03"
)
# 客厅开机的推送、客厅设定温度改成 23.0 的推送
NOTIFY_ON = bytes.fromhex("021d000d000000400700000a3400000001010003010630c9220000030701010003")
NOTIFY_TEMP = bytes.fromhex("021e000d000000ac0700000a3400000001010003010630c9220000030802e6000003")


def make_config() -> Config:
    config = Config()
    config.is_c611 = False
    config.is_d611 = True
    config.gateway_id = "gw"
    return config


class FloorHeaterDecodeTests(unittest.TestCase):
    def test_query_all_lists_every_thermostat(self):
        result, rest = decoder(QUERY_ALL, make_config())

        self.assertIsInstance(result, FloorHeaterQueryResult)
        self.assertEqual(rest, b"")
        heaters = result.heaters
        self.assertEqual([h.room_id for h in heaters], [1, 2, 3, 4, 5])
        self.assertEqual(heaters[2].mac, "30c922000003")
        self.assertEqual(heaters[4].mac, "4022d8000005")
        self.assertEqual([h.status.setted_temp for h in heaters], [24.0, 24.0, 21.0, 21.0, 21.0])
        self.assertTrue(all(h.status.switch is False for h in heaters))
        self.assertTrue(all(h.status.online is True for h in heaters))
        self.assertEqual(heaters[0].unique_id, "daikin_gw_floor_30c922000001")
        # 还没弄清含义的标签也要原样留着
        self.assertEqual(heaters[0].status.raw, {2: 1, 6: 1, 7: 0, 8: 240, 12: 0, 13: 6})

    def test_notify_carries_only_the_changed_field(self):
        on, _ = decoder(NOTIFY_ON, make_config())
        temp, _ = decoder(NOTIFY_TEMP, make_config())

        self.assertIsInstance(on, FloorHeaterNotifyResult)
        self.assertEqual(on.heater.room_id, 3)
        self.assertEqual(on.heater.mac, "30c922000003")
        self.assertIs(on.heater.status.switch, True)
        self.assertIsNone(on.heater.status.setted_temp)

        self.assertIsNone(temp.heater.status.switch)
        self.assertEqual(temp.heater.status.setted_temp, 23.0)

    def test_truncated_frame_does_not_raise(self):
        broken = bytearray(NOTIFY_ON[:-3] + b"\x03")
        broken[1] = len(broken) - 4
        result, _ = decoder(bytes(broken), make_config())
        self.assertIsInstance(result, FloorHeaterNotifyResult)


class FloorHeaterParamTests(unittest.TestCase):
    def test_query_all_frame_matches_what_the_gateway_answered(self):
        param = FloorHeaterQueryParam()
        frame = param.to_string(make_config())

        self.assertEqual(param.target, EnumDevice.FLOOR_HEATER)
        self.assertEqual(param.cmd_type, EnumCmdType.QUERY_STATUS)
        # 设备类型 0a、设备编号 34000000、指令 0300、子体 ff
        self.assertEqual(frame[11:19].hex(), "0a34000000010300")
        self.assertEqual(frame[19:].hex(), "ff03")
        self.assertEqual(frame[5], 0)  # 子体版本 0


class FloorHeaterServiceTests(unittest.TestCase):
    def _service_with_rooms(self) -> Service:
        service = Service()
        service._config = make_config()
        rooms = []
        for room_id, alias in ((1, "南卧"), (2, "北卧"), (3, "客厅"), (4, "主卧"), (5, "餐厅")):
            room = Room()
            room.id = room_id
            room.alias = alias
            rooms.append(room)
        service._rooms = rooms
        return service

    def test_first_list_creates_devices_named_after_rooms(self):
        service = self._service_with_rooms()
        result, _ = decoder(QUERY_ALL, service._config)
        result.do(service)

        heaters = service.get_floor_heaters()
        self.assertEqual([h.alias for h in heaters], ["南卧", "北卧", "客厅", "主卧", "餐厅"])

    def test_notify_merges_into_existing_status_and_calls_hook(self):
        service = self._service_with_rooms()
        decoder(QUERY_ALL, service._config)[0].do(service)
        living = service.get_floor_heaters()[2]
        calls = []
        service.register_floor_heater_hook(living, lambda: calls.append(1))

        decoder(NOTIFY_ON, service._config)[0].do(service)
        decoder(NOTIFY_TEMP, service._config)[0].do(service)

        self.assertIs(living.status.switch, True)
        self.assertEqual(living.status.setted_temp, 23.0)
        self.assertIs(living.status.online, True)   # 推送里没带的字段保持原值
        self.assertEqual(len(calls), 2)
        # 别的房间不受影响
        self.assertIs(service.get_floor_heaters()[0].status.switch, False)

    def test_later_poll_updates_status_without_replacing_devices(self):
        service = self._service_with_rooms()
        decoder(QUERY_ALL, service._config)[0].do(service)
        before = service.get_floor_heaters()
        decoder(NOTIFY_ON, service._config)[0].do(service)

        decoder(QUERY_ALL, service._config)[0].do(service)   # 轮询结果：又是全关

        self.assertIs(service.get_floor_heaters(), before)
        self.assertIs(before[2].status.switch, False)

    def test_notify_before_list_is_ignored(self):
        service = self._service_with_rooms()
        decoder(NOTIFY_ON, service._config)[0].do(service)
        self.assertEqual(service.get_floor_heaters(), [])


class FloorHeaterStatusTests(unittest.TestCase):
    def test_merge_keeps_known_values_when_other_is_empty(self):
        status = FloorHeaterStatus()
        status.switch = True
        status.setted_temp = 22.5
        status.merge(FloorHeaterStatus())
        self.assertIs(status.switch, True)
        self.assertEqual(status.setted_temp, 22.5)


if __name__ == "__main__":
    unittest.main()
