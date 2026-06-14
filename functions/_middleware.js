/**
 * Cloudflare Pages middleware — injects event-specific meta tags
 * when a request includes ?event=<id>, enabling proper social
 * sharing previews and Google indexing for each event page.
 */
export async function onRequest(context) {
  const { request, next, env } = context;
  const url = new URL(request.url);

  // Only handle requests for the root HTML page — skip all static assets
  // (JSON, images, JS, CSS) so fetch('/data/listings.json') gets real JSON.
  const pathname = url.pathname;
  if (pathname !== '/' && pathname !== '/index.html') return next();

  const eventId = url.searchParams.get('event');

  // Main page (no ?event=): inject a server-rendered event list so non-JS
  // crawlers, scrapers, and AI agents can read all listings as plain HTML.
  if (!eventId) {
    try {
      const [indexResp, dataResp] = await Promise.all([
        env.ASSETS.fetch(new Request(new URL('/', url.origin).toString())),
        env.ASSETS.fetch(new Request(new URL('/data/listings.json', url.origin).toString())),
      ]);
      const listings = await dataResp.json();
      let html = await indexResp.text();

      const items = listings.map(l => {
        const evUrl = `https://www.theknoxpulse.com/?event=${l.id}`;
        return `<li><a href="${evUrl}"><strong>${esc(l.title)}</strong></a> — ${esc(l.category)}` +
          (l.neighborhood ? ` · ${esc(l.neighborhood)}` : '') +
          (l.location ? ` · ${esc(l.location)}` : '') +
          (l.description ? `<br>${esc(l.description.slice(0,200))}` : '') +
          `</li>`;
      }).join('\n');

      const ssrBlock = `<noscript><section id="ssr-listings" style="max-width:860px;margin:2rem auto;padding:1rem;font-family:sans-serif;">` +
        `<h1>Knox Pulse — Knoxville Events</h1>` +
        `<p>Discover ${listings.length} recurring events and activities in Knoxville, TN.</p>` +
        `<ul style="line-height:2;">${items}</ul>` +
        `</section></noscript>`;

      html = html.replace('<div class="grid" id="main-grid"></div>',
        `<div class="grid" id="main-grid"></div>${ssrBlock}`);

      return new Response(html, {
        headers: { 'Content-Type': 'text/html;charset=UTF-8', 'Cache-Control': 'public, max-age=300, s-maxage=3600' }
      });
    } catch {
      return next();
    }
  }

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

    // Compute next occurrence date for startDate
    const startDate = getNextOccurrenceDate(listing);
    const endDate   = getEndDate(listing, startDate);

    // Build price/offer info
    const isFree = listing.costScale === 'Free' || !listing.costScale;
    const priceText = listing.price || (isFree ? '0' : null);
    const offers = priceText !== null ? {
      '@type': 'Offer',
      price: (priceText === 'Free' || priceText === '0') ? '0' : priceText,
      priceCurrency: 'USD',
      availability: 'https://schema.org/InStock'
    } : undefined;

    // Inject Event structured data before </head>
    const eventLd = {
      '@context': 'https://schema.org',
      '@type': 'Event',
      name: listing.title,
      description: listing.description || '',
      url: pageUrl,
      image: 'https://www.theknoxpulse.com/og-image.jpg',
      eventStatus: 'https://schema.org/EventScheduled',
      eventAttendanceMode: listing.indoorOutdoor === 'Online'
        ? 'https://schema.org/OnlineEventAttendanceMode'
        : 'https://schema.org/OfflineEventAttendanceMode',
      ...(startDate ? { startDate } : {}),
      ...(endDate   ? { endDate   } : {}),
      ...(offers    ? { offers    } : {}),
      isAccessibleForFree: isFree,
      organizer: { '@type': 'Organization', name: 'Knox Pulse', url: 'https://www.theknoxpulse.com' },
      location: {
        '@type': 'Place',
        name: listing.venueName || listing.location,
        address: {
          '@type': 'PostalAddress',
          streetAddress: listing.location,
          addressLocality: 'Knoxville',
          addressRegion: 'TN',
          addressCountry: 'US'
        },
        ...(listing.lat && listing.lng ? {
          geo: { '@type': 'GeoCoordinates', latitude: listing.lat, longitude: listing.lng }
        } : {})
      }
    };

    html = html.replace(
      '</head>',
      `<script type="application/ld+json">${JSON.stringify(eventLd)}<\/script>\n</head>`
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

/** Returns ISO date-time string for the next occurrence of this listing, or null. */
function getNextOccurrenceDate(listing) {
  const now = new Date();
  now.setHours(0, 0, 0, 0);

  if (listing.eventType === 'one-time' && listing.eventDate) {
    const t = listing.startTime || '12:00';
    return `${listing.eventDate}T${t}`;
  }

  if (listing.eventType === 'venue') {
    const t = listing.startTime || '10:00';
    return `${isoDate(now)}T${t}`;
  }

  const rule = listing.recurrenceRule;
  if (!rule) return null;

  // Search up to 60 days out for next occurrence
  for (let i = 0; i < 60; i++) {
    const d = new Date(now); d.setDate(d.getDate() + i);
    const ds = isoDate(d);
    const dow = d.getDay();
    const dom = d.getDate();
    const month = d.getMonth();
    let matches = false;
    switch (rule.type) {
      case 'weekly':
        matches = rule.days.includes(dow); break;
      case 'weekly-seasonal':
        matches = rule.days.includes(dow)
          && (!rule.months || !rule.months.length || rule.months.includes(month))
          && (!rule.seasonStart || ds >= rule.seasonStart)
          && (!rule.seasonEnd || ds <= rule.seasonEnd);
        break;
      case 'monthly-weekday':
        matches = dow === rule.day && Math.ceil(dom / 7) === rule.weekNum; break;
      case 'annual':
        matches = month === rule.month; break;
    }
    if (matches) {
      const t = listing.startTime || '12:00';
      return `${isoDate(d)}T${t}`;
    }
  }
  return null;
}

/** Returns end date-time for schema.org/Event endDate. */
function getEndDate(listing, startDate) {
  // Multi-day one-time event: end is last day at endTime (or 23:59)
  if (listing.eventType === 'one-time' && listing.eventEndDate) {
    const t = listing.endTime || '23:59';
    return `${listing.eventEndDate}T${t}`;
  }
  // Seasonal window: use seasonEnd date if available
  if (listing.recurrenceRule?.seasonEnd) {
    const t = listing.endTime || listing.startTime || '23:59';
    return `${listing.recurrenceRule.seasonEnd}T${t}`;
  }
  if (!startDate || !listing.endTime) return null;
  return startDate.replace(/T[\d:]+$/, `T${listing.endTime}`);
}

function isoDate(d) {
  const mm = String(d.getMonth() + 1).padStart(2, '0');
  const dd = String(d.getDate()).padStart(2, '0');
  return `${d.getFullYear()}-${mm}-${dd}`;
}

function esc(str) {
  return (str || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}
