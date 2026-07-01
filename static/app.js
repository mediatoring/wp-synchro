// Global mode-badge sync on page load
document.addEventListener('DOMContentLoaded', () => {
  const liveSwitch = document.getElementById('live-mode');
  if (liveSwitch) {
    liveSwitch.addEventListener('change', () => {
      const live = liveSwitch.checked;
      const mb = document.getElementById('mode-badge');
      if (mb) {
        mb.textContent = live ? '⚡ OSTRÝ REŽIM' : '● DRY-RUN';
        mb.className = 'badge fs-6 px-3 py-2 ' + (live ? 'bg-danger' : 'bg-info');
      }
    });
  }
});
