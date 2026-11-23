# Govee BLE Lighting Integration for HomeAssistant

![Govee Logo](assets/govee-logo.png)

A powerful and seamless integration to control your Govee lighting devices via Govee API or BLE directly from HomeAssistant with full features support.

---

## Table of Contents

- [Features](#features)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
- [Support & Contribution](#support--contribution)
- [License](#license)

---

## Features

- 🚀 **Direct BLE Control**: No need for middlewares or bridges. Connect and control your Govee devices directly through Bluetooth Low Energy.

- ☁️ **API Control**: Supported all light devices with full features support including scenes!

- 🌈 **Scene Selection**: Leverage the full potential of your Govee lights by choosing from all available scenes, transforming the ambiance of your room instantly.
  
- 💡 **Comprehensive Lighting Control**: Adjust brightness, change colors, or switch on/off with ease.

---

## Configuration

### What is needed

For Direct BLE Control:
- Before you begin, make certain HomeAssistant can access BLE on your platform. Ensure your HomeAssistant instance is granted permissions to utilize the Bluetooth Low Energy of your host machine.

For Govee API Control:
- Retrieve Govee-API-Key as described [here](https://developer.govee.com/reference/apply-you-govee-api-key), setup integration with API type ad fill your API key.

## Usage

With the integration setup, your Govee devices will appear as entities within HomeAssistant. All you need to do is select your device model when adding it.

---

## Troubleshooting for BLE

If you're facing issues with the integration, consider the following steps:

1. **Check BLE Connection**: 
   
   Ensure that the Govee device is within the Bluetooth range of your HomeAssistant host machine.

2. **Model Check**:

   Check that you selected correct device model.

3. **Logs**:

   HomeAssistant logs can provide insights into any issues. Navigate to `Configuration > Logs` to review any error messages related to the Govee integration.

---

## Support & Contribution

- **Found an Issue?** 
   
   Raise it in the [Issues section](https://github.com/Beshelmek/govee_ble_lights/issues) of this repository.

- **Device support**:

   Almost every Govee device has its own BLE message protocol. If you have an Android smartphone and your device is not supported, please contact me on [Telegram](https://t.me/Beshelmek).

- **Contributions**:

   We welcome community contributions! If you'd like to improve the integration or add new features, please fork the repository and submit a pull request.

---

## Future Plans

We aim to continuously improve this integration by:

- Supporting more Govee device models for BLE
- Enhancing the overall user experience and stability

---

## License

This project is under the MIT License. For full license details, please refer to the [LICENSE file](https://github.com/Beshelmek/govee_ble_lights/blob/main/LICENSE) in this repository.

---

## Fork notes (weirded/govee_ble_lights)

BLE path rewritten around a tested protocol module (`protocol.py`) and a
connection manager (`device.py`):

- **Real state.** Polls the light over BLE (`AA 01/04/05/A5` queries on the
  `…2b10` notify characteristic) for power, brightness, mode/scene and
  per-segment brightness + colour, instead of assuming the last command.
- **One reused connection** per light, released after 45 s idle (upstream
  opened a new GATT connection per command and never closed it, exhausting
  proxy slots).
- **Colour temperature** (2000-9000 K) on segmented models.
- **Scenes fixed** for models whose payload is in `lightEffects[].scenceParam`
  (H6072 had an empty effect list), and the scene code (`33 05 04`) is sent
  after the payload. The active scene is read back from the light.
- **Brightness scale** 0-100 for H6072 (verified via `AA 04`).
- **`govee-ble-lights.set_segments`** service: per-segment colour,
  colour temperature and brightness.

Verified on an H6072 (hardware 1.00.01/1.00.12, firmware 1.03.05).
Run protocol tests: `python3 tests/test_protocol.py custom_components/govee-ble-lights`
