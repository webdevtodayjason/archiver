# Follow-ups

Filed here because the Dart MCP needs an OAuth round trip and these should not wait on
it. Migrate to the project's dartboard when it is authorised.

---

## Entity index: three measured weaknesses in the subject pass

**Owner:** Jason
**Added:** 2026-09-13
**Nonblocking because:** LAST LIGHT answers correctly today. The refusal test passes with
zero inventions, "what is a deadfall" returns a cited field-manual entry across nine
books, and the cockpit draws the library in 2 seconds. Every item below makes a good
result better; none of them makes a wrong one right.

**Next action:** rewrite `subjects()` in `entities.py` to handle stemming and speech
artefacts, then `python3 entities.py subjects` against `~/lastlight/corpus` to rebuild the
2,500-row index. Items 1 and 2 both need that same rebuild, so do them together.

**Proof of closure:** `python3 entities.py top 40` on the LAST LIGHT corpus shows no
`gonna`/`knowed`/`wadn`, and no pair of singular/plural forms of the same word. The shelf
panel's "What it is about" for the survival shelf leads with subjects rather than
`water, keep, sure, ground, side`.

### 1. Morphological variants split the index and fake the strongest edges

`joists`/`joist`, `quilt`/`quilts`, `canner`/`canners`, `antiviral`/`antivirals` are
separate rows. Worse than untidy: they form the **strongest co-occurrence edges in the
graph with each other**, because a document using one almost always uses the other. The
map spends its best links on a word and its own plural.

Same class of bug as the case fold (`HOLACE`/`HoLaCe`) already fixed in `fold_case()`, and
probably the same shape of fix, applied to a stem rather than a lowercase key. Note the
case fold keeps the most frequent surface form; a stem fold should do the same rather than
showing people a stem.

### 2. Foxfire transcribed speech is indexed as subject matter

`gonna`, `knowed`, `wadn`, `feller`, `hollered` are all indexed subjects. They come from
the Foxfire books, which transcribe Appalachian speech verbatim, and they are bursty in
exactly the way a real subject is: heavy use in a few documents, absent everywhere else.

The existing dropped-g guard catches `somethin` and `goin` by checking whether the
spelled-out word is in the corpus and commoner. It does not catch these, because `knowed`
and `wadn` are not a dropped `g`. This was measured as the worst seeding population at
document scale: `gonna, lawton, knowed, wadn, feller` were the top seeds.

### 3. Shelf "what it is about" ranks by document count, not aboutness

The survival shelf currently leads with `water, keep, sure, ground, side`. Ranking by
concentration times log-rarity instead gives `influenza, radiological, foxfire, pandemic,
antiviral`, which is plainly better for that shelf and plainly worse for Vikidia, where it
degrades to `yusuf, normal-type, salado, minoans`.

**Deliberately not fixed** for that reason. A change that improves one shelf and wrecks
another is not ready, and the honest version probably picks a ranking per shelf the same
way `_shape()` now picks a graph unit per corpus. The same weakness affects how the shelf
constellation seeds its nodes.

---

## Cockpit: three smaller things found while fixing the library view

**Owner:** Jason
**Added:** 2026-09-13
**Nonblocking because:** none of these produce a wrong answer. Two are cosmetic and one is
a performance footgun that is currently harmless.

- **`DF_CEILING` is inert.** It removes 0 of 7,858 entities on LAST LIGHT, because
  `subjects()` already applies the same 12% ceiling at build time and no name reaches 725
  documents. Harmless, but the docstring implies it is doing work. Either delete it or say
  what it is really for.
- **Thundering herd on boot.** The page fires three requests that each want `_graph()`
  before the cache fills, so all three compute it. At 1.7s that is invisible; it was the
  difference between 834s and 2,500s before the join was fixed. One lock would end it.
- **The badge says READY over an empty canvas.** It reports layout settling, not whether
  any data arrived. On the corpus where nothing rendered, it still said READY, which is
  the one moment it needed to say something else.

**Proof of closure:** a comment or a fix for each. The badge one only matters if a corpus
can still render empty; if it cannot, say so and delete the item.
