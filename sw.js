// 참돔파닥파닥 웹앱 서비스워커: 화면 파일 오프라인 보관 + 알림 눌렀을 때 해당 종목 열기
const CACHE = 'chamdom-v9e31494';
const SHELL = ['./', 'index.html', 'manifest.json', 'icon-192.png', 'icon-512.png', 'apple-touch-icon.png'];
self.addEventListener('install', e => { e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting())); });
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const u = new URL(e.request.url);
  if (u.origin !== location.origin || e.request.method !== 'GET') return;     // 업비트 시세는 항상 새로 받음
  e.respondWith(fetch(e.request).then(r => { const cp = r.clone(); caches.open(CACHE).then(c => c.put(e.request, cp)); return r; })
    .catch(() => caches.match(e.request, {ignoreSearch: true})));            // 인터넷 없으면 저장된 화면
});
self.addEventListener('notificationclick', e => {
  e.notification.close();
  const m = (e.notification.data || {}).m || '';
  e.waitUntil(self.clients.matchAll({type: 'window', includeUncontrolled: true}).then(cs => {
    for (const c of cs) { c.postMessage({open: m}); return c.focus(); }
    return self.clients.openWindow('./' + (m ? '?m=' + encodeURIComponent(m) : ''));
  }));
});
