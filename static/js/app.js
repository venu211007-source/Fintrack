// Global utilities for FinTrack

// Auto-uppercase currency fields
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('input.text-uppercase').forEach(el => {
    el.addEventListener('input', () => { el.value = el.value.toUpperCase(); });
  });

  // Auto-dismiss alerts after 5s
  document.querySelectorAll('.alert.alert-success').forEach(el => {
    setTimeout(() => {
      const a = bootstrap.Alert.getOrCreateInstance(el);
      if (a) a.close();
    }, 5000);
  });
});

// Utility: format number with commas
function fmt(n, dec = 2) {
  return Number(n).toLocaleString(undefined, { minimumFractionDigits: dec, maximumFractionDigits: dec });
}

// Utility: POST JSON fetch
async function postJSON(url, data) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  return res.json();
}
