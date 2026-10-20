// Runs before first paint so a saved theme never flashes the wrong one.
try {
  const theme = localStorage.getItem("aihub.theme");
  if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
} catch {
  // Storage can be blocked; the system theme applies.
}
