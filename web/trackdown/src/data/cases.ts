// Simulated historical trackdowns. Every account, outlet and domain here is fictional
// (domains end in .example) and every case is flagged `synthetic: true` in the UI.
import type { Cluster, TraceCase } from "../types";
import rhinoCopy from "../assets/rhino-copy.webp";
import rhino1515 from "../assets/rhino-1515.webp";
import rhinoPrint from "../assets/rhino-print.webp";
import { Builder, DAY, HOUR, MIN, SEC, T } from "./build";

const Y = (y: number, m = 0, d = 1) => Date.UTC(y, m, d);

/** A real one: Dürer's Rhinoceros (1515). Written by hand from public history; the only
 *  invented node is the present-day repost, which is labelled as illustrative. */
function rhinoceros(): TraceCase {
  const b = new Builder(1515);
  const origin = b.add({
    t: Y(1515, 4, 20),
    platform: "letter",
    role: "origin",
    author: { name: "The animal itself", handle: "Lisbon, 20 May 1515", hue: 40 },
    text: "An Indian rhinoceros, a gift for King Manuel I of Portugal, arrives in Lisbon. The first one seen alive in Europe in centuries.",
  });
  const letter = b.add({
    t: Y(1515, 5, 1),
    platform: "letter",
    role: "relay",
    author: { name: "A letter from Lisbon", handle: "sender unknown", hue: 30 },
    text: "A written description and a sketch of the animal travel to Nuremberg. Both are now lost.",
    flags: ["lost"],
  });
  b.link(origin, letter, "quote", { tool: "archive", note: "described in writing" });
  const drawing = b.add({
    t: Y(1515, 6, 1),
    platform: "print",
    role: "mutation",
    author: { name: "Albrecht Dürer", handle: "pen drawing, Nuremberg", hue: 25 },
    text: "Dürer draws the rhinoceros in pen and ink from the letter and the sketch.",
    flags: ["never saw the animal"],
  });
  b.link(letter, drawing, "copy", { tool: "archive", note: "drawn from the description" });
  const woodcut = b.add({
    t: Y(1515, 8, 1),
    platform: "print",
    role: "mutation",
    author: { name: "Albrecht Dürer", handle: "woodcut, Nuremberg", hue: 25 },
    text: "The woodcut. Printed again and again, it becomes the picture of a rhinoceros.",
    media: { kind: "plate", src: rhino1515, title: "Rhinocerus, woodcut, 1515" },
    flags: ["invented: armour plates", "invented: small horn on the back"],
  });
  b.link(drawing, woodcut, "copy", { tool: "archive" });
  const burgkmair = b.add({
    t: Y(1515, 9, 1),
    platform: "print",
    role: "organic",
    author: { name: "Hans Burgkmair", handle: "woodcut, Augsburg", hue: 140 },
    text: "A second woodcut the same year, from a separate account. Closer to the real animal, and far less copied.",
  });
  b.link(origin, burgkmair, "quote", { tool: "archive", note: "a separate account" });
  const wreck = b.add({
    t: Y(1516, 0, 15),
    platform: "letter",
    role: "source",
    author: { name: "Shipwreck off Italy", handle: "early 1516", hue: 210 },
    text: "Sent on as a gift to Pope Leo X, the rhinoceros drowns when its ship goes down. Nobody else gets to look.",
  });
  b.link(origin, wreck, "cites", { tool: "archive" });
  const gessner = b.add({
    t: Y(1551, 0, 1),
    platform: "print",
    role: "relay",
    author: { name: "Conrad Gessner", handle: "Historiae animalium, Zürich", hue: 60 },
    text: "The great natural history of the age uses Dürer's image as its rhinoceros.",
    media: { kind: "plate", src: rhinoCopy, title: "Stand-in image: the woodcut, coarsened to suggest a copy" },
  });
  b.link(woodcut, gessner, "media", { tool: "archive", note: "the woodcut, copied" });
  const textbooks = b.add({
    t: Y(1930, 0, 1),
    platform: "print",
    role: "relay",
    author: { name: "German school textbooks", handle: "early 20th century", hue: 0 },
    text: "Still printed as a true picture of the animal, four hundred years on.",
    media: { kind: "plate", src: rhinoPrint, title: "Stand-in image: the woodcut, reprinted" },
  });
  b.link(woodcut, textbooks, "media", { tool: "archive" });
  const sub = b.add({
    t: T("2026-11-12T19:20:00Z"),
    platform: "bluesky",
    role: "submitted",
    author: { name: "A repost today", handle: "illustrative.example", hue: 330 },
    text: "found this 1515 rhino drawing. people really thought rhinos came with armour lmao",
    media: { kind: "plate", src: rhino1515, title: "the same woodcut" },
    flags: ["illustrative post"],
  });
  b.link(woodcut, sub, "media", { tool: "archive" });
  return {
    id: "rhinoceros",
    title: "Dürer's Rhinoceros, 1515",
    kicker: "Historical record",
    status: "likely_organic",
    confidence: 0,
    scored: false,
    synthetic: false,
    historical: true,
    submittedId: sub,
    originId: origin,
    nodes: b.nodes,
    edges: b.edges,
    clusters: [],
    summary: "The most-copied wrong picture in history. Every copy goes back to one artist who never saw the animal. Wrong, yes. Coordinated, no.",
    receipts: [
      { tool: "archive", text: "Dürer never saw the rhinoceros. He worked from a written description and a sketch sent from Lisbon." },
      { tool: "archive", text: "His woodcut adds things the animal didn't have: plates like armour, and a small twisted horn on its back." },
      { tool: "archive", text: "Hans Burgkmair's woodcut from the same year is closer to life, but Dürer's became the standard picture for centuries, down to school textbooks in the 20th century." },
      { tool: "archive", text: "Wrong is not the same as coordinated. The copies came from different printers over four centuries, with no sign of a common push." },
    ],
    wouldChange: ["Many printers releasing the same image at once, paid by one patron to make a point"],
    steps: [
      { tool: "you", text: "submitted: a repost of an old print", found: [sub] },
      { tool: "archive", text: "image matches Dürer's woodcut, 1515", hop: 1, found: [woodcut], rejected: 3 },
      { tool: "archive", text: "the woodcut follows his own pen drawing", hop: 2, found: [drawing], rejected: 0 },
      { tool: "archive", text: "the drawing follows a letter from Lisbon, now lost", hop: 3, found: [letter], rejected: 2 },
      { tool: "archive", text: "the original: the animal itself, Lisbon, 1515", hop: 4, found: [origin], rejected: 0 },
    ],
  };
}

function tapWater(): TraceCase {
  const b = new Builder(7);
  const origin = b.add({
    t: T("2021-06-02T09:14:00Z"),
    platform: "news",
    role: "origin",
    author: { name: "The Daily Spoon", handle: "dailyspoon.example", hue: 38, ageDays: 3100, followers: 88000 },
    text: "City council votes to ban tap water on weekends “to give the pipes a rest”",
    media: { kind: "link", title: "SATIRE · Council: pipes ‘deserve the weekend off’", domain: "dailyspoon.example", hue: 38 },
    url: "https://dailyspoon.example/2021/06/pipes-weekend",
    likes: 2100,
    reposts: 340,
    flags: ["labelled satire"],
  });
  const fc = b.add({
    t: T("2021-06-02T09:20:00Z") + 3 * DAY,
    platform: "factcheck",
    role: "source",
    author: { name: "Daily Spoon · About", handle: "dailyspoon.example/about", hue: 210 },
    text: "“Everything on this site is made up. If you shared it as news, that one's on you.”",
    url: "https://dailyspoon.example/about",
  });
  b.link(origin, fc, "cites", { tool: "oxylabs", note: "site's own satire disclaimer" });

  const forum = b.add({
    t: T("2021-06-03T22:40:00Z"),
    platform: "forum",
    role: "mutation",
    author: { name: "pipe_dreamer", handle: "u/pipe_dreamer", hue: 260, ageDays: 1400, followers: 210 },
    text: "Wow, they are actually banning tap water now. Unreal. Weekend = no water.",
    media: { kind: "link", title: "Council: pipes ‘deserve the weekend off’", domain: "dailyspoon.example" },
    likes: 940,
    reposts: 112,
    flags: ["satire label dropped"],
  });
  b.link(origin, forum, "quote", { sim: 0.71, tool: "oxylabs", note: "same headline, “SATIRE” tag removed" });

  const shot = b.add({
    t: T("2024-02-11T08:05:00Z"),
    platform: "bluesky",
    role: "mutation",
    author: { name: "City Leaks", handle: "cityleaks.bsky.social", hue: 200, ageDays: 210, followers: 3400 },
    text: "They buried this one. Weekend tap water ban is real, look it up.",
    media: { kind: "screenshot", title: "Council: pipes deserve the weekend off", domain: "screenshot", hue: 8, note: "Nemotron: same article, masthead and SATIRE tag cropped out" },
    likes: 1800,
    reposts: 620,
    flags: ["masthead cropped"],
  });
  b.link(forum, shot, "media", { sim: 0.94, tool: "nemotron", note: "same article image, masthead cropped" });

  for (let i = 0; i < 3; i++) {
    const id = b.add({
      t: T("2024-02-11T09:00:00Z") + i * 3 * HOUR + b.int(0, 50) * MIN,
      platform: "bluesky",
      role: "relay",
      author: b.person(),
      text: ["wait is this real?", "Can't believe this passed.", "lol this is from a satire site, people"][i],
      likes: b.int(3, 60),
      reposts: b.int(0, 12),
    });
    b.link(shot, id, i === 2 ? "reply" : "quote", { tool: "detector" });
  }

  // 2026 burst: one seed, 30 copies within 94 s
  const t0 = T("2026-11-13T20:41:07Z");
  const seedAuthor = b.fresh({ name: "Truth Wave Daily", handle: "truthwave.daily.bsky.social", ageDays: 6, followers: 11 });
  const burstText = "They're banning tap water on weekends. NOT a joke. Share before it gets deleted 👇";
  const seed = b.add({
    t: t0,
    platform: "bluesky",
    role: "amplifier",
    author: seedAuthor,
    text: burstText,
    media: { kind: "screenshot", title: "Council: pipes deserve the weekend off", domain: "screenshot", hue: 8, note: "Nemotron: identical to the 2024 screenshot" },
    cluster: "c1",
    likes: 9,
    reposts: 31,
    flags: ["acct 6 d old", "11 followers"],
  });
  b.link(shot, seed, "media", { sim: 0.99, tool: "nemotron", note: "identical image hash family" });
  const members = [seed];
  let submitted = "";
  for (let i = 0; i < 30; i++) {
    const a = b.fresh();
    const dt = Math.round((94 * SEC * (i + 1)) / 30 + (b.r() - 0.5) * 1500);
    const textVar = b.r() < 0.8 ? burstText : burstText.replace("NOT a joke", "not a joke!!");
    const id = b.add({
      t: t0 + dt,
      platform: "bluesky",
      role: i === 17 ? "submitted" : "amplifier",
      author: a,
      text: textVar,
      media: { kind: "screenshot", title: "Council: pipes deserve the weekend off", domain: "screenshot", hue: 8 },
      cluster: "c1",
      likes: b.int(0, 4),
      reposts: b.int(0, 3),
      flags: [`acct ${a.ageDays} d old`, `${a.followers} followers`],
    });
    if (i === 17) submitted = id;
    members.push(id);
    b.link(seed, id, "copy", { sim: textVar === burstText ? 1 : 0.96, tool: "detector" });
  }
  const reacts = [
    ["is this for real?? my city??", "organic"],
    ["This is a 2021 satire article. Check the masthead they cropped.", "organic"],
    ["ok who is posting this exact same text 30 times", "organic"],
  ] as const;
  reacts.forEach(([text, role], i) => {
    const id = b.add({ t: t0 + (6 + i * 9) * MIN, platform: "bluesky", role, author: b.person(), text, likes: b.int(10, 220), reposts: b.int(1, 40) });
    b.link(members[2 + i * 7], id, "quote", { tool: "detector" });
  });

  const clusters: Cluster[] = [{ id: "c1", label: "Coordinated burst", status: "coordinated", members, stat: "31 accounts · same image + text · 94 s" }];
  return {
    id: "tap-water",
    title: "The weekend tap-water ban",
    kicker: "Recycled satire",
    status: "coordinated",
    confidence: 0.91,
    synthetic: true,
    submittedId: submitted,
    originId: origin,
    nodes: b.nodes,
    edges: b.edges,
    clusters,
    summary: "A 2021 satire headline lost its label on a forum, got its masthead cropped in 2024, then 31 accounts, all only days old, posted it within 94 seconds.",
    receipts: [
      { tool: "detector", text: "31 accounts posted the same image and text within 94 s. Timing permutation test: 0 of 500 shuffled worlds were this synchronized (p < 0.002)." },
      { tool: "detector", text: "Median account age 5 days, median 6 followers. 29 of 31 accounts never posted before tonight." },
      { tool: "nemotron", text: "The image is the 2021 article with the masthead and the “SATIRE” tag cropped out (visual match 0.94)." },
      { tool: "oxylabs", text: "Earliest copy found: dailyspoon.example, 2 Jun 2021, a satire site that says everything it posts is made up.", url: "https://dailyspoon.example/2021/06/pipes-weekend" },
      { tool: "gemini", text: "Same claim, same crop, same wording, from accounts with no history, inside 94 s. The spread is coordinated; the claim itself is satire presented as news." },
      { tool: "detector", against: true, text: "3 replies from long-standing accounts look independent, and one of them debunks it. They are not part of the cluster." },
    ],
    wouldChange: ["The burst accounts turn out to have years of normal history elsewhere", "Timing spread over hours instead of seconds"],
    steps: [
      { tool: "you", text: "Post submitted: text + screenshot", found: [submitted] },
      { tool: "nemotron", text: "Read the screenshot: headline OCR, layout fingerprint, crop edges", hop: 0 },
      { tool: "detector", text: "Matched 30 posts with the same text in a 94 s window", hop: 1, found: [seed], rejected: 6 },
      { tool: "nemotron", text: "Same image posted 11 Feb 2024 (match 0.99)", hop: 2, found: [shot], rejected: 4 },
      { tool: "oxylabs", text: "/search on the headline: forum thread, Jun 2021", hop: 3, found: [forum], rejected: 9 },
      { tool: "oxylabs", text: "/scrape: original article, labelled SATIRE, 2 Jun 2021", hop: 4, found: [origin, fc], rejected: 3 },
      { tool: "gemini", text: "Verdict written from 6 receipts" },
    ],
  };
}

function bridge(): TraceCase {
  const b = new Builder(21);
  const t0 = T("2026-11-12T17:58:20Z");
  const origin = b.add({
    t: t0,
    platform: "bluesky",
    role: "origin",
    author: { name: "Kaisa Films", handle: "kaisa.films.bsky.social", hue: 160, ageDays: 1900, followers: 2300 },
    text: "Harbour bridge just closed both ways, police everywhere, you could hear a loud bang from the east pillar. Filmed from the ferry.",
    media: { kind: "video", title: "0:41 · view from the ferry", hue: 200, note: "Nemotron: no reuse found; first appearance" },
    likes: 5400,
    reposts: 2100,
  });
  const news = b.add({
    t: t0 + 42 * MIN,
    platform: "news",
    role: "source",
    author: { name: "Harbour Post", handle: "harbourpost.example", hue: 215, ageDays: 5000, followers: 120000 },
    text: "Harbour bridge closed after expansion joint failure; no injuries reported",
    media: { kind: "link", title: "Bridge closed after joint failure", domain: "harbourpost.example", hue: 215 },
    url: "https://harbourpost.example/live/bridge-closure",
  });
  b.link(origin, news, "cites", { tool: "oxylabs", note: "article embeds the ferry video" });
  const quotes = [
    "Can confirm, stuck on the 14 bus, nothing moving",
    "Heard it from the east shore, thought it was thunder",
    "Anyone know if the ferries are still running?",
    "That joint was flagged in the 2023 inspection report btw",
    "Hope everyone is okay 🙏",
    "Detour via the ring road is already jammed",
    "ok the video is wild",
    "Officials say no injuries, per Harbour Post",
  ];
  const level1: string[] = [];
  quotes.forEach((q, i) => {
    const id = b.add({
      t: t0 + (6 + i * 9 + b.int(0, 6)) * MIN,
      platform: "bluesky",
      role: "organic",
      author: b.person(),
      text: q,
      likes: b.int(20, 900),
      reposts: b.int(2, 120),
    });
    b.link(i === 7 ? news : origin, id, i % 3 === 0 ? "quote" : "repost", { tool: "detector" });
    level1.push(id);
  });
  let submitted = "";
  level1.forEach((p, i) => {
    const n = b.int(0, 2);
    for (let k = 0; k < n; k++) {
      const id = b.add({
        t: b.nodes.find((x) => x.id === p)!.t + b.int(8, 70) * MIN,
        platform: "bluesky",
        role: i === 3 && k === 0 ? "submitted" : "organic",
        author: b.person(),
        text: b.pick(["same here", "my cousin is on it right now", "this is why we need maintenance budgets", "this ↓", "wow", "Ugh, my commute"]),
        likes: b.int(1, 120),
        reposts: b.int(0, 20),
      });
      if (i === 3 && k === 0) submitted = id;
      b.link(p, id, b.r() < 0.5 ? "quote" : "repost", { tool: "detector" });
    }
  });
  if (!submitted) submitted = level1[3];
  for (let i = 0; i < 9; i++) {
    const id = b.add({ t: t0 + (50 + i * 14 + b.int(0, 9)) * MIN, platform: "bluesky", role: "organic", author: b.person(), text: b.pick(["Bridge closed, no injuries — via Harbour Post", "Live updates here", "Good to hear nobody got hurt", "Avoid the bridge tonight"]), media: { kind: "link", title: "Bridge closed after joint failure", domain: "harbourpost.example" }, likes: b.int(2, 80), reposts: b.int(0, 30) });
    b.link(news, id, "link", { tool: "detector" });
  }
  const members = b.nodes.filter((n) => n.role === "organic" || n.role === "submitted" || n.id === origin).map((n) => n.id);
  return {
    id: "bridge",
    title: "Harbour bridge closure",
    kicker: "Breaking news burst",
    status: "likely_organic",
    confidence: 0.84,
    synthetic: true,
    submittedId: submitted,
    originId: origin,
    nodes: b.nodes,
    edges: b.edges,
    clusters: [{ id: "c2", label: "Breaking-news burst", status: "likely_organic", members, stat: `${members.length} accounts · varied text · over 3 h` }],
    summary: "A ferry passenger's video, then a news write-up, then people reacting in their own words over three hours. Loud, fast, and organic.",
    receipts: [
      { tool: "detector", text: "Timing permutation test: 156 of 500 shuffled worlds were at least this synchronized (p = 0.31). Not unusual." },
      { tool: "detector", text: "Accounts are 1–7 years old with ordinary follower counts. Text overlap between posts is low (median 0.08)." },
      { tool: "nemotron", text: "The ferry video has no earlier copies; frames match the news article's embed." },
      { tool: "oxylabs", text: "Harbour Post confirms the closure 42 min after the first post and cites the video.", url: "https://harbourpost.example/live/bridge-closure" },
      { tool: "gemini", text: "A real-world event drove many independent people to post at once. Burst, yes. Coordination, no." },
      { tool: "detector", against: true, text: "9 accounts shared the same news link inside 30 min. Expected for breaking news; the organic guard scored it 0.82." },
    ],
    wouldChange: ["Several accounts posting identical wording", "A cluster of new accounts appearing in the first minutes"],
    steps: [
      { tool: "you", text: "Post submitted: quote of a quote", found: [submitted] },
      { tool: "detector", text: "Walked the quote chain up 2 hops", hop: 1, found: [level1[3]], rejected: 2 },
      { tool: "nemotron", text: "Video frames match: ferry clip, first seen 17:58", hop: 2, found: [origin], rejected: 1 },
      { tool: "oxylabs", text: "/search: news coverage 42 min later, cites the clip", hop: 3, found: [news], rejected: 5 },
      { tool: "gemini", text: "Verdict written from 6 receipts" },
    ],
  };
}

function flood(): TraceCase {
  const b = new Builder(5);
  const origin = b.add({
    t: T("2015-10-03T07:30:00Z"),
    platform: "news",
    role: "origin",
    author: { name: "Riverside Courier", handle: "riversidecourier.example", hue: 25, ageDays: 6200, followers: 54000 },
    text: "Photos: overnight flooding in the old riverside district",
    media: { kind: "photo", title: "Flooded street, riverside district", hue: 205, domain: "riversidecourier.example" },
    url: "https://riversidecourier.example/2015/10/flooding-photos",
  });
  const blog = b.add({
    t: T("2019-04-18T15:10:00Z"),
    platform: "forum",
    role: "relay",
    author: { name: "weatherbuff", handle: "u/weatherbuff", hue: 120, ageDays: 2000, followers: 90 },
    text: "Throwback to the 2015 floods. Never forget.",
    media: { kind: "photo", title: "Flooded street (2015)", hue: 205 },
  });
  b.link(origin, blog, "media", { sim: 0.98, tool: "nemotron", note: "same photo, resized" });
  const mut = b.add({
    t: T("2026-11-12T19:02:00Z"),
    platform: "bluesky",
    role: "mutation",
    author: { name: "Henri Salo", handle: "henri.salo.bsky.social", hue: 290, ageDays: 900, followers: 610 },
    text: "This is happening RIGHT NOW in the east district and the media is silent!!",
    media: { kind: "photo", title: "Flooded street", hue: 205, note: "Nemotron: same photo as 2015, watermark removed" },
    likes: 330,
    reposts: 140,
    flags: ["date context changed"],
  });
  b.link(blog, mut, "media", { sim: 0.97, tool: "nemotron", note: "same photo, watermark removed" });
  let submitted = "";
  for (let i = 0; i < 5; i++) {
    const id = b.add({
      t: T("2026-11-12T19:30:00Z") + i * 7 * HOUR + b.int(0, 120) * MIN,
      platform: "bluesky",
      role: i === 1 ? "submitted" : "relay",
      author: b.person(),
      text: ["omg stay safe everyone", "Why is nobody reporting this??", "sharing for visibility", "my aunt lives there", "!!!"][i],
      likes: b.int(5, 90),
      reposts: b.int(1, 30),
    });
    if (i === 1) submitted = id;
    b.link(mut, id, "repost", { tool: "detector" });
  }
  const deb = b.add({ t: T("2026-11-13T08:15:00Z"), platform: "bluesky", role: "organic", author: b.person(), text: "Reverse image search says this is from 2015. Not today.", likes: 210, reposts: 88 });
  b.link(mut, deb, "reply", { tool: "detector" });
  return {
    id: "flood",
    title: "Flood photo, wrong decade",
    kicker: "Out-of-context photo",
    status: "insufficient",
    confidence: 0.5,
    synthetic: true,
    submittedId: submitted,
    originId: origin,
    nodes: b.nodes,
    edges: b.edges,
    clusters: [],
    summary: "An 11-year-old news photo was reposted as happening “right now”. Misleading, yes. Coordinated? Six accounts over two days isn't enough to say.",
    receipts: [
      { tool: "nemotron", text: "Same photo as Riverside Courier, 3 Oct 2015 (match 0.97); the watermark in the corner was removed." },
      { tool: "oxylabs", text: "Earliest copy: riversidecourier.example photo gallery, 2015.", url: "https://riversidecourier.example/2015/10/flooding-photos" },
      { tool: "detector", text: "Only 6 accounts over 2 days. Too few shared actions to run the timing test; abstaining." },
      { tool: "gemini", text: "The caption is false context. That is a content problem, not proof of coordination. We don't stretch the evidence." },
      { tool: "detector", against: true, text: "Accounts are ordinary and posted hours apart in their own words." },
    ],
    wouldChange: ["The same caption showing up from many accounts in a short window", "More reposts so the timing test can run"],
    steps: [
      { tool: "you", text: "Post submitted: repost with photo", found: [submitted] },
      { tool: "detector", text: "Repost of a 12 Nov post", hop: 1, found: [mut], rejected: 0 },
      { tool: "nemotron", text: "Photo seen in a 2019 forum post (match 0.97)", hop: 2, found: [blog], rejected: 7 },
      { tool: "oxylabs", text: "/search + /scrape: news gallery, 3 Oct 2015", hop: 3, found: [origin], rejected: 4 },
      { tool: "detector", text: "Too few accounts for a timing test: abstain" },
    ],
  };
}

function token(): TraceCase {
  const b = new Builder(99);
  const t0 = T("2026-11-13T06:00:00Z");
  const origin = b.add({
    t: t0,
    platform: "web",
    role: "origin",
    author: { name: "GLOW Foundation", handle: "glow-partners.example", hue: 280, ageDays: 3, followers: 0 },
    text: "$GLOW announces strategic partnership with a top-3 global payments network",
    media: { kind: "link", title: "PRESS: $GLOW x Global Payments", domain: "glow-partners.example", hue: 280, note: "page first seen 4 h before the first post" },
    url: "https://glow-partners.example/press/partnership",
    flags: ["page first seen 4 h earlier"],
  });
  const none = b.add({
    t: t0 + 9 * HOUR,
    platform: "factcheck",
    role: "source",
    author: { name: "Payments network newsroom", handle: "paynet.example/newsroom", hue: 210 },
    text: "Newsroom search for “GLOW”: 0 results. No partnership announcement.",
    url: "https://paynet.example/newsroom?q=GLOW",
  });
  b.link(origin, none, "cites", { tool: "oxylabs", note: "no matching announcement" });
  const clusters: Cluster[] = [];
  let submitted = "";
  const wave = (cid: string, start: number, n: number, label: string) => {
    const mem: string[] = [];
    for (let i = 0; i < n; i++) {
      const num = 400 + b.int(0, 99) + i;
      const id = b.add({
        t: start + i * 30 * SEC + b.int(-2, 2) * SEC,
        platform: "bluesky",
        role: cid === "w2" && i === 9 ? "submitted" : "amplifier",
        author: { name: `GLOW fan ${num}`, handle: `glow_fan_0${num}.bsky.social`, hue: 280 + b.int(-12, 12), ageDays: b.int(2, 4), followers: b.int(0, 6), templated: true },
        text: b.pick(["🚀 $GLOW x Global Payments is HUGE. Early holders win. #GLOW #crypto", "$GLOW partnership confirmed 🚀🚀 not financial advice #GLOW", "Huge news for $GLOW holders 🚀 #GLOW #web3"]),
        media: { kind: "link", title: "PRESS: $GLOW x Global Payments", domain: "glow-partners.example" },
        cluster: cid,
        likes: b.int(0, 3),
        reposts: b.int(0, 2),
        flags: ["templated handle", "every 30 s"],
      });
      if (cid === "w2" && i === 9) submitted = id;
      mem.push(id);
      b.link(origin, id, "link", { tool: "detector" });
    }
    clusters.push({ id: cid, label, status: "coordinated", members: mem, stat: `${n} accounts · posts every 30 ± 2 s` });
    return mem;
  };
  wave("w1", t0 + 12 * MIN, 16, "Wave 1");
  for (let i = 0; i < 3; i++) {
    const id = b.add({ t: t0 + (3 + i * 2) * HOUR, platform: "bluesky", role: "organic", author: b.person(), text: ["is this legit? can't find it on the payments site", "these replies are all from glow_fan accounts lmao", "rugpull speedrun"][i], likes: b.int(20, 300), reposts: b.int(1, 40) });
    b.link(origin, id, "link", { tool: "detector" });
  }
  wave("w2", t0 + 7 * HOUR, 20, "Wave 2");
  return {
    id: "glow",
    title: "The $GLOW “partnership”",
    kicker: "Token pump",
    status: "coordinated",
    confidence: 0.95,
    synthetic: true,
    submittedId: submitted,
    originId: origin,
    nodes: b.nodes,
    edges: b.edges,
    clusters,
    summary: "A press page nobody had seen before, then 36 templated accounts posting it every 30 seconds, in two waves seven hours apart.",
    receipts: [
      { tool: "detector", text: "36 accounts named glow_fan_0###, created 2–4 days ago, posting at a fixed 30 ± 2 s rhythm in two waves." },
      { tool: "detector", text: "Timing permutation test: 0 of 500 shuffled worlds were this regular (p < 0.002)." },
      { tool: "oxylabs", text: "The payments network's newsroom has no such announcement.", url: "https://paynet.example/newsroom?q=GLOW" },
      { tool: "oxylabs", text: "The press page was first seen 4 h before the first post.", url: "https://glow-partners.example/press/partnership" },
      { tool: "gemini", text: "Templated identities, clockwork timing and an unconfirmed claim. This is a coordinated push." },
      { tool: "detector", against: true, text: "3 sceptical replies come from established accounts and are not part of the cluster." },
    ],
    wouldChange: ["The payments network confirms the partnership", "The accounts turn out to be one person's openly labelled bot"],
    steps: [
      { tool: "you", text: "Post submitted: link + hashtags", found: [submitted] },
      { tool: "detector", text: "20 templated accounts, 30 s rhythm, same link", hop: 1, rejected: 4 },
      { tool: "oxylabs", text: "/scrape the link: glow-partners.example press page", hop: 2, found: [origin], rejected: 0 },
      { tool: "detector", text: "Same link pushed 7 h earlier by 16 more accounts", hop: 1 },
      { tool: "oxylabs", text: "/search the claim on the payments newsroom: nothing", hop: 3, found: [none], rejected: 6 },
      { tool: "gemini", text: "Verdict written from 6 receipts" },
    ],
  };
}

let cache: TraceCase[] | null = null;
export function simulatedCases(): TraceCase[] {
  return (cache ??= [tapWater(), rhinoceros(), bridge(), flood(), token()]);
}
