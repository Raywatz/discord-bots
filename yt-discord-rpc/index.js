const RPC = require('discord-rpc');
const http = require('http');
require('./music-server');

const CLIENT_ID = '1493767467531501628';

let client = new RPC.Client({ transport: 'ipc' });

let albumArtCache = {};
let currentVideoId = null;
let presenceInterval = null;

const DEVICES = ['macbook', 'phone', 'slash-rig'];
const PAUSE_TIMEOUT = 60 * 1000;

const deviceState = {};
DEVICES.forEach(d => {
  deviceState[d] = {
    online: false,
    title: null,
    artist: null,
    currentTime: 0,
    duration: 0,
    paused: true,
    playingSince: null,
    lastUpdate: null,
    pausedAt: null,
  };
});

const server = http.createServer((req, res) => {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');

  if (req.method === 'OPTIONS') {
    res.writeHead(204);
    res.end();
    return;
  }

  if (req.method === 'POST' && req.url === '/update') {
    let body = '';
    req.on('data', chunk => body += chunk);
    req.on('end', () => {
      try {
        const info = JSON.parse(body);
        const device = info.device;

        if (!device || !DEVICES.includes(device)) {
          res.writeHead(400);
          res.end('unknown device');
          return;
        }

        if (info.videoId) {
          currentVideoId = info.videoId;
        }

        updateDevice(device, info);
      } catch {}
      res.writeHead(200);
      res.end('ok');
    });
  } else {
    res.writeHead(404);
    res.end();
  }
});

server.listen(43211, '0.0.0.0', () => {
  console.log('🌐 Bridge server listening on port 43211');
});

function updateDevice(device, info) {
  const prev = deviceState[device];
  const now = Date.now();

  if (info.title !== prev.title) {
    deviceState[device].playingSince = now;
  }

  deviceState[device].online = true;
  deviceState[device].title = info.title;
  deviceState[device].artist = info.artist;
  deviceState[device].currentTime = info.currentTime;
  deviceState[device].duration = info.duration;
  deviceState[device].paused = info.paused;
  deviceState[device].lastUpdate = now;

  if (!info.paused) {
    deviceState[device].pausedAt = null;
  } else if (!prev.paused && info.paused) {
    deviceState[device].pausedAt = now;
  }
}

function checkOffline() {
  const now = Date.now();
  DEVICES.forEach(d => {
    const state = deviceState[d];
    if (state.online && state.lastUpdate && now - state.lastUpdate > 30000) {
      console.log(`📴 ${d} went offline`);
      deviceState[d].online = false;
    }
  });
}

function pickDevice() {
  const now = Date.now();
  const online = DEVICES.filter(d => deviceState[d].online && deviceState[d].title);
  const playing = online.filter(d => !deviceState[d].paused);

  if (playing.length > 0) {
    return playing.reduce((best, d) => {
      const bPlayingSince = deviceState[best].playingSince || now;
      const dPlayingSince = deviceState[d].playingSince || now;
      return dPlayingSince < bPlayingSince ? d : best;
    });
  }

  const paused = online.filter(d => deviceState[d].paused);
  const recentlyPaused = paused.filter(d => {
    const pausedAt = deviceState[d].pausedAt;
    return pausedAt && now - pausedAt < PAUSE_TIMEOUT;
  });

  if (recentlyPaused.length > 0) return recentlyPaused[0];

  return null;
}

async function getAlbumArt(artist, title) {
  const cacheKey = `${artist}::${title}`;
  if (albumArtCache[cacheKey]) return albumArtCache[cacheKey];

  try {
    const query = encodeURIComponent(`${artist} ${title}`);
    const res = await fetch(`https://itunes.apple.com/search?term=${query}&media=music&limit=1`);
    const data = await res.json();
    const art = data?.results?.[0]?.artworkUrl100?.replace('100x100', '600x600');
    if (art) {
      albumArtCache[cacheKey] = art;
      return art;
    }
  } catch {}

  return null;
}

function getYTMusicUrl(videoId) {
  if (!videoId) return null;
  return `https://yt-redirect-coral.vercel.app/ytmusic?v=${videoId}`;
}

function getSpotifyUrl(artist, title) {
  const q = encodeURIComponent(`${title} ${artist}`);
  return `https://yt-redirect-coral.vercel.app/spotify?q=${q}`;
}
async function updatePresence() {
  checkOffline();
  const device = pickDevice();

  if (!device) {
    try { await client.clearActivity(); } catch {}
    return;
  }

  const { title, artist, currentTime, duration, paused } = deviceState[device];

  if (paused) {
    try { await client.clearActivity(); } catch {}
    return;
  }

  const nowMs = Date.now();
  const startTimestamp = new Date(nowMs - currentTime * 1000);

  const albumArt = await getAlbumArt(artist, title);
  const ytMusicUrl = getYTMusicUrl(currentVideoId);
  const spotifyUrl = getSpotifyUrl(artist, title);

  console.log(`🎵 [${device}] ${artist} - ${title} (${Math.floor(currentTime)}s / ${Math.floor(duration)}s)`);

  const buttons = [
    ...(ytMusicUrl ? [{ label: 'Listen on YouTube Music', url: ytMusicUrl }] : []),
    ...(spotifyUrl ? [{ label: 'Listen on Spotify', url: spotifyUrl }] : [])
  ].slice(0, 2);

  try {
    await client.setActivity({
      details: title,
      state: artist || 'YouTube Music',
      startTimestamp,
      largeImageKey: albumArt || 'youtube_music',
      largeImageText: title,
      smallImageKey: 'youtube_music',
      smallImageText: 'YouTube Music',
      instance: false,
      ...(buttons.length > 0 ? { buttons } : {})
    });
  } catch (err) {
    // Transient discord-rpc IPC errors (Discord restarting, pipe hiccup)
    // shouldn't crash the whole process — this runs on a 1s setInterval
    // with no other rejection handler.
    console.log('⚠️ setActivity failed:', err.message || err);
  }
}

client.on('ready', () => {
  console.log(`✅ Connected to Discord`);
  console.log('🎵 Watching Now Playing...\n');
  if (presenceInterval) clearInterval(presenceInterval);
  presenceInterval = setInterval(updatePresence, 1000);
});

async function connect() {
  try {
    await client.login({ clientId: CLIENT_ID });
  } catch (err) {
    console.log('❌ Discord connection failed, retrying in 10s...');
    try { await client.destroy(); } catch {}
    client = new RPC.Client({ transport: 'ipc' });
    client.on('ready', () => {
      console.log('✅ Reconnected to Discord');
      if (presenceInterval) clearInterval(presenceInterval);
      presenceInterval = setInterval(updatePresence, 1000);
    });
    client.on('disconnected', () => {
      console.log('❌ Discord disconnected, retrying in 10s...');
      setTimeout(connect, 10000);
    });
    setTimeout(connect, 10000);
  }
}

client.on('disconnected', () => {
  console.log('❌ Discord disconnected, retrying in 10s...');
  setTimeout(connect, 10000);
});

connect();