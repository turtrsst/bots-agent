// PM2: jalankan bridge HTTP + dashboard + webhook Telegram 24/7.
//
//   pm2 start ecosystem.config.js
//   pm2 save && pm2 startup      # hidup lagi setelah reboot
//   pm2 logs kra-server
//
// Catatan: satu proses ini sudah menjalankan daemon (scheduler ideasi, watcher
// input_creator/, antrean render, perintah Telegram) sekaligus dashboard di :8100.
// Ganti `KRA_ROOT` bila folder studio bukan folder repo ini.
module.exports = {
  apps: [
    {
      name: "kra-server",
      cwd: __dirname,
      script: ".venv/bin/python",
      args: "virtual_office_superbots.py --serve --host 0.0.0.0 --port 8100",
      interpreter: "none",
      autorestart: true,
      max_restarts: 20,
      restart_delay: 5000,
      kill_timeout: 15000,
      out_file: "logs/kra-server.out.log",
      error_file: "logs/kra-server.err.log",
      time: true,
      env: {
        PYTHONUNBUFFERED: "1",
        STUDIO_ASYNC_GATE: "1", // WAJIB: tidak ada input() di server
        // STUDIO_ROOT: "/path/ke/kantor-rama-ai",
        // STUDIO_YOUTUBE_API_KEY: "AIza...",
        // STUDIO_TELEGRAM_TOKEN: "7123456789:AAH...",
        // STUDIO_TELEGRAM_CHAT_ID: "987654321",
      },
    },
  ],
};
