/**
 * Cloudflare Pages middleware — injects event-specific meta tags
 * when a request includes ?event=<id>, enabling proper social
 * sharing previews and Google indexing for each event page.
 */
export async function onRequest(context) {
  const { request, next, env } = context;
  const url = new URL(request.url);
  const eventId = url.searchParams.get('event');

  if (!eventId) return next();

  try {
    // Load static listing data
    const dataResp = await env.ASSETS.fetch(
      new Request(new URL('/data/listings.json', url.origin).toString())
    );
    const listings = await dataResp.json();
    const listing = listings.find(l => l.id === eventId);

    if (!listing) return next();

    // Fetch the base SPA page
    const indexResp = await env.ASSETS.fetch(
      new Request(new URL('/', url.origin).toString())
    );
    let html = await indexResp.text();

    const title     = `${listing.title} — Knox Pulse Knoxville`;
    const desc      = (listing.description || '').slice(0, 200);
    const pageUrl   = `https://www.theknoxpulse.com/?event=${eventId}`;
    const descEsc   = esc(desc);
    const titleEsc  = esc(listing.title);

    // Replace head meta tags with event-specific values
    html = html
      .replace(/<title>[^<]*<\/title>/, `<title>${esc(title)}</title>`)
      .replace(/<meta name="description"[^>]*\/>/, `<meta name="description" content="${descEsc}"/>`)
      .replace(/<link rel="canonical"[^>]*\/>/, `<link rel="canonical" href="${pageUrl}"/>`)
      .replace(/<meta property="og:type"[^>]*\/>/, `<meta property="og:type" content="website"/>`)
      .replace(/<meta property="og:url"[^>]*\/>/, `<meta property="og:url" content="${pageUrl}"/>`)
      .replace(/<meta property="og:title"[^>]*\/>/, `<meta property="og:title" content="${titleEsc} — Knox Pulse"/>`)
      .replace(/<meta property="og:description"[^>]*\/>/, `<meta property="og:description" content="${descEsc}"/>`)
      .replace(/<meta name="twitter:title"[^>]*\/>/, `<meta name="twitter:title" content="${titleEsc} — Knox Pulse"/>`)
      .replace(/<meta name="twitter:description"[^>]*\/>/, `<meta name="twitter:description" content="${descEsc}"/>`);

    // Inject Event structured data before </head>
    const eventLd = {
      '@context': 'https://schema.org',
      '@type': 'Event',
      name: listing.title,
      description: listing.description || '',
      location: {
        '@type': 'Place',
        name: listing.location,
        ...(listing.lat && listing.lng ? {
          geo: { '@type': 'GeoCoordinates', latitude: listing.lat, longitude: listing.lng }
        } : {})
      },
      url: pageUrl,
      organizer: { '@type': 'Organization', name: 'Knox Pulse', url: 'https://www.theknoxpulse.com' }
    };

    html = html.replace(
      '</head>',
      `<script type="application/ld+json">${JSON.stringify(eventLd)}</script>\n</head>`
    );

    return new Response(html, {
      headers: {
        'Content-Type': 'text/html;charset=UTF-8',
        'Cache-Control': 'public, max-age=300, s-maxage=3600',
      }
    });
  } catch {
    return next();
  }
}

function esc(str) {
  return (str || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}
