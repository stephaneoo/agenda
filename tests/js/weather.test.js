import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { forecastDays, weatherIcon } from "../../web/static/web/weather.js";

const payload = {
  daily_units: { temperature_2m_min: "°C", temperature_2m_max: "°C" },
  daily: {
    time: ["2026-11-05", "2026-11-06", "2026-11-07", "2026-11-08", "2026-11-09"],
    temperature_2m_min: [2.1, 4, 6, 1, 3],
    temperature_2m_max: [14.4, 13, 11, 15, 14],
    weather_code: [0, 2, 61, 3, 95],
  },
};

test("forecast shows validated temperatures and condition icons for five days", () => {
  const days = forecastDays(payload, "2026-11-05");
  assert.equal(days.length, 5);
  assert.deepEqual(days[0], { date: "2026-11-05", minimum: 2, maximum: 14, icon: ["sun", "Ensoleillé"] });
  assert.deepEqual(weatherIcon(71), ["snowflake", "Neige"]);
  assert.deepEqual(weatherIcon(45), ["cloud-fog", "Brouillard"]);
  assert.equal(weatherIcon(900), null);
});

test("weather PNG contains seven native-size 8px icons", () => {
  const png = readFileSync(new URL("../../web/static/web/weather-icons.png", import.meta.url));
  assert.deepEqual(png.subarray(0, 8), Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]));
  assert.equal(png.readUInt32BE(16), 56);
  assert.equal(png.readUInt32BE(20), 8);
});

test("saved forecast never shows expired days", () => {
  assert.equal(forecastDays(payload, "2026-11-06").length, 4);
  assert.deepEqual(forecastDays(payload, "2026-11-10"), []);
});

test("invalid weather is rejected rather than turned into fabricated temperatures", () => {
  assert.deepEqual(forecastDays(null, "2026-11-05"), []);
  assert.deepEqual(forecastDays({ ...payload, daily_units: {} }, "2026-11-05"), []);
  assert.deepEqual(forecastDays({ ...payload, daily: { ...payload.daily, weather_code: [] } }, "2026-11-05"), []);
  const invalid = structuredClone(payload);
  invalid.daily.temperature_2m_min[0] = null;
  invalid.daily.temperature_2m_max[1] = -30;
  invalid.daily.weather_code[2] = 900;
  assert.deepEqual(forecastDays(invalid, "2026-11-05").map((day) => day.date), ["2026-11-08", "2026-11-09"]);
});