module.exports = {
  apps: [
    {
      name: "youtube-dl-web",
      script: ".venv/bin/gunicorn",
      args: "-b 0.0.0.0:8000 app:app",
      cwd: __dirname,
      interpreter: "none",
      env_file: ".env",
    },
  ],
};
