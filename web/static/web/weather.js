import { addDays, parisToday } from "./filter.js";

const URL = "https://api.open-meteo.com/v1/forecast?latitude=44.360&longitude=5.141&daily=weather_code,temperature_2m_min,temperature_2m_max&timezone=Europe%2FParis&forecast_days=5";
const CACHE = "agenda-weather-nyons-v1";
const DAY = new Intl.DateTimeFormat("fr-FR", { weekday: "short", timeZone: "Europe/Paris" });

export function weatherIcon(code) {
  if (code === 0) return ["sun", "Ensoleillé"];
  if (code === 1 || code === 2) return ["cloud-sun", "Éclaircies"];
  if (code === 3) return ["cloud", "Couvert"];
  if (code === 45 || code === 48) return ["cloud-fog", "Brouillard"];
  if ([71, 73, 75, 77, 85, 86].includes(code)) return ["snowflake", "Neige"];
  if ([95, 96, 99].includes(code)) return ["cloud-lightning", "Orage"];
  if ([51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82].includes(code)) return ["cloud-rain", "Pluie"];
  return null;
}

export function forecastDays(payload, today = parisToday()) {
  const daily = payload?.daily;
  if (!Array.isArray(daily?.time) || daily.time.length > 10
      || !["temperature_2m_min", "temperature_2m_max", "weather_code"].every((key) => Array.isArray(daily[key]) && daily[key].length === daily.time.length)
      || payload.daily_units?.temperature_2m_min !== "°C" || payload.daily_units?.temperature_2m_max !== "°C") return [];
  return daily.time.flatMap((date, index) => {
    const minimum = daily.temperature_2m_min[index];
    const maximum = daily.temperature_2m_max[index];
    const icon = weatherIcon(daily.weather_code[index]);
    if (![0, 1, 2, 3, 4].some((offset) => date === addDays(today, offset)) || !icon
        || !Number.isFinite(minimum) || !Number.isFinite(maximum) || minimum < -90 || maximum > 70 || minimum > maximum) return [];
    return [{ date, minimum: Math.round(minimum), maximum: Math.round(maximum), icon }];
  });
}

function render(container, days, savedAt, cached) {
  const items = days.map(({ date, minimum, maximum, icon: [name, description] }) => {
    const item = document.createElement("span");
    item.className = "weather-day";
    const label = date === parisToday() ? "auj." : DAY.format(new Date(`${date}T12:00:00+01:00`)).replace(".", "");
    item.setAttribute("aria-label", `${label} : ${description}, ${minimum} à ${maximum} degrés`);
    const icon = document.createElement("span");
    icon.className = "weather-icon";
    icon.dataset.weather = name;
    icon.setAttribute("aria-hidden", "true");
    item.append(icon, `${label} ${minimum}°/${maximum}°`);
    return item;
  });
  if (cached) {
    const note = document.createElement("span");
    note.textContent = "enregistrée";
    items.push(note);
  }
  container.replaceChildren(...items);
  container.title = `Prévisions Nyons · Open-Meteo · ${cached ? "enregistrées" : "actualisées"} ${new Date(savedAt).toLocaleString("fr-FR")}`;
  container.dataset.state = cached ? "cached" : "live";
}

export async function loadWeather() {
  const container = document.getElementById("weather");
  if (!container) return;
  let cached;
  try { cached = JSON.parse(localStorage.getItem(CACHE)); } catch {}
  const days = forecastDays(cached?.payload);
  const age = Date.now() - cached?.savedAt;
  const usable = days.length && Number.isFinite(age) && age >= 0;
  if (usable) {
    render(container, days, cached.savedAt, true);
    if (navigator.onLine && age < 30 * 60 * 1000 && days.length === 5) {
      render(container, days, cached.savedAt, false);
      return;
    }
  }
  try {
    if (!navigator.onLine) throw new Error("offline");
    const response = await fetch(URL, { credentials: "omit", referrerPolicy: "no-referrer", signal: AbortSignal.timeout(6000) });
    if (!response.ok) throw new Error("weather unavailable");
    const payload = await response.json();
    const forecast = forecastDays(payload);
    if (!forecast.length) throw new Error("invalid forecast");
    const savedAt = Date.now();
    try { localStorage.setItem(CACHE, JSON.stringify({ payload, savedAt })); } catch {}
    render(container, forecast, savedAt, false);
  } catch {
    if (!usable) {
      container.textContent = "Météo indisponible";
      container.dataset.state = "unavailable";
    }
  }
}