export function formatCount(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "0";
  return Math.trunc(number).toLocaleString("en-US");
}

export function formatCountLabel(value, singular, plural = `${singular}s`) {
  const number = Number(value);
  const label = number === 1 ? singular : plural;
  return `${formatCount(number)} ${label}`;
}
