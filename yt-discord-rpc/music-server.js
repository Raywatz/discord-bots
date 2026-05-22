const http = require('http');
const { execFile } = require('child_process');

const YTDLP = '/opt/homebrew/bin/yt-dlp';

function run(args) {
  return new Promise((resolve, reject) => {
    execFile(YTDLP, args, { maxBuffer: 50 * 1024 * 1024 }, (err, stdout) => {
      if (err) reject(err);
      else resolve(stdout.trim());
    });
  });
}

const server = http.createServer(async (req, res) => {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Content-Type', 'application/json');

  const url = new URL(req.url, 'http://localhost');

  if (url.pathname === '/search') {
    const q = url.searchParams.get('q');
    if (!q) {
      res.writeHead(400);
      res.end(JSON.stringify({ error: 'missing query' }));
      return;
    }

    try {
      const output = await run(
        [`ytmsearch5:${q}`, '--dump-json', '--no-playlist', '--skip-download']
      );

      const results = output
        .split('\n')
        .filter(Boolean)
        .map(line => {
          try {
            const d = JSON.parse(line);
            return {
              id: d.id,
              title: d.title,
              artist: d.channel || d.uploader || '',
              duration: d.duration || 0,
              thumbnail: d.thumbnail || '',
            };
          } catch {
            return null;
          }
        })
        .filter(Boolean);

      res.writeHead(200);
      res.end(JSON.stringify(results));
    } catch (err) {
      res.writeHead(500);
      res.end(JSON.stringify({ error: 'search failed' }));
    }

  } else if (url.pathname === '/stream') {
    const v = url.searchParams.get('v');
    if (!v) {
      res.writeHead(400);
      res.end(JSON.stringify({ error: 'missing video id' }));
      return;
    }

    try {
      const streamUrl = await run(
        [`https://music.youtube.com/watch?v=${v}`, '--get-url', '-f', 'bestaudio']
      );

      res.writeHead(200);
      res.end(JSON.stringify({ url: streamUrl }));
    } catch (err) {
      res.writeHead(500);
      res.end(JSON.stringify({ error: 'stream failed' }));
    }

  } else {
    res.writeHead(404);
    res.end(JSON.stringify({ error: 'not found' }));
  }
});

server.listen(43213, '0.0.0.0', () => {
  console.log('🎵 Music server listening on port 43213');
});

module.exports = server;