# Model B TeleLogger bring-up

- [x] Identify the USB serial interface and ESP32 flash size.
- [x] Capture the current firmware serial output.
- [x] Build the unmodified upstream TeleLogger firmware.
- [ ] Save a verified recovery backup from the connected device.
- [x] Add an ignored local configuration for Wi-Fi, the server, SPIFFS, and Simbase.
- [ ] Build and flash the Wi-Fi-first firmware.
- [ ] Verify Wi-Fi association and end-to-end telemetry reception.
- [ ] Verify SIM detection, LTE registration, and telemetry reception through Simbase.
- [ ] Verify OBD-II data with the device connected to a vehicle.

Current hardware constraints:

- The OBD-II connector is not connected to a vehicle.
- No microSD card is installed. Use the ESP32 internal SPIFFS partition.
- The SIM must be inserted while the ONE+ is powered off, with its gold contacts facing the printed lid.
