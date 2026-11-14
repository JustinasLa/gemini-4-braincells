# Historic test posts for `/api/analyze`

Reusable Twitter/X and Facebook inputs for manual regression tests of the analyze box and
`POST /api/analyze`. The cases cover historic coordinated campaigns, organic viral events,
repeated copypasta, URL normalization, short text, and links blocked by Facebook's login wall.

## What to expect

The detector only sees Bluesky activity in the live window (48 h raw retention), so **every one
of these returns `status: "insufficient"` with the "not in window" note**. What they test is
the live web check (`web_findings`, Oxylabs + Gemini) and the exact-phrase text search:

- A non-Bluesky URL is matched against stored cluster links (no hit for old posts) and then sent
  to the web check.
- Text needs 5+ words. `claim_phrase` searches only the **longest sentence**, cut to 12 words,
  so paste just the key sentence of a copypasta.
- `normalize_url` does not treat `x.com` and `twitter.com` as the same host.

Each call makes paid live Oxylabs/Gemini calls; nothing is stored.

## Coordinated campaigns (historic record should surface; no bot claim about real people)

| Input | Date | State on X | Why |
|---|---|---|---|
| `https://twitter.com/ten_gop/status/788098438915297280` | 2016-10-17 | suspended | IRA account @TEN_GOP. Text: `This is sickening. Hillary using the "Mentally Ill" to incite violence at Trump rallies. #FreeJulian #BirdDogging #PodestaEmails10`. [Archive](https://web.archive.org/web/20161210150428/https://twitter.com/ten_gop/status/788098438915297280), [The Outline](https://theoutline.com/post/3397/here-are-the-most-shared-tweets-created-by-russian-trolls-that-twitter-scrubbed-from-the-internet) |
| `https://twitter.com/ten_gop/status/788978081293819904` | 2016-10-20 | suspended | IRA @TEN_GOP. Text: `BREAKING🚨 Hillary shuts down press conference when asked about DNC Operatives corruption & #VoterFraud #debatenight #TrumpBookReport`. [Archive](https://web.archive.org/web/20170729133226/https://twitter.com/ten_gop/status/788978081293819904) |
| `https://twitter.com/TEN_GOP/status/785663973613899776` | 2016-10-11 | suspended | IRA @TEN_GOP; text not recovered. [DFRLab](https://dfrlab.org/2017/11/14/how-a-russian-troll-fooled-america/) |
| `https://twitter.com/Crystal1Johnson/status/785614679682846720` | 2016-10-10 | suspended | IRA persona aimed at Black audiences. Text: `When It’s slowly becoming illegal for black people to work #BlackTwitter`. [Archive](https://web.archive.org/web/20161201040034/https://twitter.com/Crystal1Johnson/status/785614679682846720) |
| `https://twitter.com/LeroyLovesUSA/status/796772863869075460` | 2016-11-11 | suspended | IRA "Goodbye murderer" Obama banner stunt. [Archive](https://web.archive.org/web/20161112145800/https://twitter.com/LeroyLovesUSA/status/796772863869075460), [Wikipedia: IRA](https://en.wikipedia.org/wiki/Internet_Research_Agency) |
| `https://twitter.com/JackPosobiec/status/860567072010620929` | 2017-05-05 | deleted by author | Start of #MacronLeaks. [DFRLab](https://dfrlab.org/2017/05/05/hashtag-campaign-macronleaks/) |
| `https://twitter.com/williamcraddick/status/860569224212697088` | 2017-05-05 | live | Early #MacronLeaks post: `New Leak Reveals Emails, Documents From Macron and Affiliated Staff Members #MacronLeaks`. Describe the campaign; don't call the person a bot. |
| `https://twitter.com/wikileaks/status/860577607670276096` | 2017-05-05 | live | Real account, main amplifier: `Alleged multi-GB team Macron email archives. Could be a 4chan practical joke. We are examining` |
| `https://twitter.com/Cyrilefevre/status/860775639686803456` | 2017-05-06 | live | Mocks Le Pen inside the same hashtag: `En exclusivité, le nouveau logo du FHaine. #MacronLeaks`. Hashtag ≠ membership. [DFRLab follow-up](https://dfrlab.org/2017/05/06/macronleaks-campaign-hits-resistance/) |
| text: `YES IM BLACK AND IM VOTING FOR TRUMP!!!` | 2020-10 | accounts suspended | Exact string from fake-persona accounts. [Al Jazeera/Reuters](https://www.aljazeera.com/news/2020/10/13/twitter-suspends-fake-accounts-of-alleged-black-trump-supporters) |

## Copypasta (many identical copies, different causes)

| Text | Origin |
|---|---|
| `The sharing of biased and false news has become all too common on social media.` | 2018 Sinclair must-run script, read by dozens of local stations (top-down). [Truthout](https://truthout.org/articles/viral-video-shows-sinclair-forcing-local-tv-anchors-to-recite-trumpian-propaganda/) |
| `This is extremely dangerous to our democracy.` | Press-quoted form of the same script; the script says "**a** democracy", so it tests wording drift. [PBS](https://www.pbs.org/newshour/show/viral-video-raises-worry-over-sinclairs-political-messaging-inside-local-news) |
| `I do not give Facebook or any entities associated with Facebook permission to use my pictures, information, messages or posts, both past and future.` | Facebook privacy-notice hoax since 2012, spread person to person, not bots. [PolitiFact](https://politifact.com/factchecks/2019/may/01/facebook-posts/rumor-about-facebook-going-public-your-posts-and-p/) |
| `I HEREBY STATE THAT I DO NOT GIVE MY PERMISSION.` | Closing line of the same hoax (2019 version). |
| `An attorney advised us to post this.` | Opening of the 2019 version. |

Sinclair vs the Facebook hoax is the best pair: both are mass repetition, one ordered, one organic.

## Organic viral controls (should stay calm)

| Input | Date | Note |
|---|---|---|
| `https://twitter.com/jack/status/20` and `https://x.com/jack/status/20` | 2006-03-21 | First tweet, `just setting up my twttr` (exactly 5 words). Compare the two hosts. |
| `https://twitter.com/chrismessina/status/223115412` | 2007-08-23 | First hashtag proposal; account now protected, post unreadable. |
| `https://twitter.com/jkrums/status/1121915133` | 2009-01-15 | Hudson River plane: `http://twitpic.com/135xa - There's a plane in the Hudson. I'm on the ferry going to pick up the people. Crazy.` (dead twitpic link) |
| `https://twitter.com/ReallyVirtual/status/64780730286358528` | 2011-05-01 | Bin Laden raid live-tweeted by accident: `Helicopter hovering above Abbottabad at 1AM (is a rare event).` |
| `https://twitter.com/BarackObama/status/266031293945503744` | 2012-11-07 | `Four more years.` (use the URL; text is under 5 words) |
| `https://twitter.com/Oreo/status/298246571718483968` | 2013-02-04 | `Power out? No problem.` Planned brand marketing, not fake accounts. "You can still dunk in the dark" is image text. |
| `https://twitter.com/TheEllenShow/status/440322224407314432` | 2014-03-03 | Oscars selfie, record retweets: `If only Bradley's arm was longer. Best photo ever. #oscars` |
| `https://twitter.com/Deadspin/status/980175772206993409` | 2018-03-31 | Journalism exposing the Sinclair script; not its source. |
| `https://twitter.com/HillaryClinton/status/861317789537193988` | 2017-05-07 | Talks about "interfering w/democracy"; topic words shouldn't trigger a flag. |
| `https://twitter.com/realDonaldTrump/status/869858333477523458` | 2017-05-31 | Live covfefe follow-up; pair with the deleted original below. |

## Edge cases

- **Deleted vs suspended:** IRA posts above show "suspended account"; covfefe
  `https://twitter.com/realDonaldTrump/status/869766994899468288` shows "deleted by the Post author"
  ([archive](https://web.archive.org/web/20170531054122/https://twitter.com/realDonaldTrump/status/869766994899468288)).
- **Too-short text:** `Ed Balls` (`https://twitter.com/edballs/status/63623585020915713`, 2011) and
  `Four more years.` sent as text should get the "too short" note.
- **Facebook login wall** (plain fetches got a login or "Temporarily Blocked" page):
  - `https://www.facebook.com/zuck/posts/10104712037900071`: 2018 Cambridge Analytica statement.
  - `https://www.facebook.com/zuck/posts/10103460278231481`: 2017 immigration post.
  - `https://www.facebook.com/zuck/videos/10101586925575221/`: 2014 Ice Bucket Challenge.
  - `https://www.facebook.com/candaceSpayne/videos/10209653193067040/`: 2016 Chewbacca Mom.
  - `https://www.facebook.com/photo.php?fbid=882818462056827&set=a.148852332120114&theater=&type=3`: 2019 privacy hoax specimen.

## Ready to paste

```bash
API=https://noise-radar.srv2159172.hstgr.cloud/api/analyze
curl -s -X POST $API -H 'Content-Type: application/json' -d '{"url":"https://twitter.com/ten_gop/status/788098438915297280"}'
curl -s -X POST $API -H 'Content-Type: application/json' -d '{"url":"https://twitter.com/TheEllenShow/status/440322224407314432"}'
curl -s -X POST $API -H 'Content-Type: application/json' -d '{"text":"The sharing of biased and false news has become all too common on social media."}'
curl -s -X POST $API -H 'Content-Type: application/json' -d '{"text":"I do not give Facebook or any entities associated with Facebook permission to use my pictures, information, messages or posts, both past and future."}'
curl -s -X POST $API -H 'Content-Type: application/json' -d '{"url":"https://x.com/jack/status/20"}'
```
