import assert from "node:assert/strict";
import test from "node:test";

import {
  DEFAULT_SORT,
  TOP_RATED_FILTERS,
  activeFilterChips,
  catalogueStateFromSearch,
  catalogueUrlSearch,
  discreteRangeValues,
  filtersFor,
  filtersFromPreset,
  filtersMatch,
  filtersWithoutChip,
  formatFreshness,
  hasConflictingRangeFilters,
  itemContentType,
  itemMetadata,
  namedValues,
  nearestRangeIndex,
  presetsFor,
  queryString,
  randomQueryString,
  rangeSelectionLabel,
  responsiveFilterPanelClasses,
  safeExternalUrl,
  serviceFaviconUrl,
  scoreLabel,
  sortOptionsFor,
  streamingServiceBrand,
  streamingServiceEntries,
  usesTopRatedAnimeHomepage,
  validatedPage,
  visiblePageNumbers,
} from "../src/catalogue.js";


test("clear filters are visually blank while the anime homepage stays TV-ranked", () => {
  const cleared = filtersFor("ANIME");

  assert.equal(cleared.type, "");
  assert.equal(cleared.status, "");
  assert.equal(cleared.genre.length, 0);
  assert.equal(cleared.tag.length, 0);
  assert.equal(cleared.studio.length, 0);
  assert.equal(cleared.streaming_service.length, 0);
  assert.equal(cleared.max_chapters, "");
  assert.equal(cleared.max_volumes, "");
  assert.equal(TOP_RATED_FILTERS.type, "TV");
  assert.equal(filtersMatch(cleared, filtersFor("MANGA")), true);
});

test("explicitly choosing all anime types leaves the TV-only homepage", () => {
  const cleared = filtersFor("ANIME");

  assert.equal(usesTopRatedAnimeHomepage("ANIME", cleared), true);
  assert.equal(usesTopRatedAnimeHomepage("ANIME", cleared, true), false);
});

test("unrated catalogue cards display a question mark beside their star", () => {
  assert.equal(scoreLabel(null), "?");
  assert.equal(scoreLabel(undefined), "?");
  assert.equal(scoreLabel(""), "?");
  assert.equal(scoreLabel(8.125), "8.13");
});

test("anime queries send anime filters and omit readable-title filters", () => {
  const filters = {
    ...filtersFor(),
    q: "monster",
    type: "TV",
    season: "spring",
    status: "CURRENTLY_AIRING",
    min_episodes: "12",
    min_chapters: "50",
    genre: ["Drama", "Mystery"],
    exclude_genre: ["Isekai"],
    exclude_tag: ["harem"],
    studio: ["Bones", "Madhouse"],
    streaming_service: ["Crunchyroll", "Netflix"],
    author: ["Hiromu Arakawa"],
  };
  const params = new URLSearchParams(queryString(filters, 3, "ANIME"));

  assert.equal(params.get("content_type"), "ANIME");
  assert.equal(params.get("page"), "3");
  assert.equal(params.get("preview"), "1");
  assert.equal(params.get("type"), "TV");
  assert.equal(params.get("season"), "spring");
  assert.equal(params.get("status"), "CURRENTLY_AIRING");
  assert.equal(params.get("min_episodes"), "12");
  assert.equal(params.get("genre"), "Drama,Mystery");
  assert.equal(params.get("exclude_genre"), "Isekai");
  assert.equal(params.get("exclude_tag"), "harem");
  assert.equal(params.get("studio"), "Bones,Madhouse");
  assert.equal(params.get("streaming_service"), "Crunchyroll,Netflix");
  assert.equal(params.has("author"), false);
  assert.equal(params.get("sort"), DEFAULT_SORT);
  assert.equal(params.has("min_chapters"), false);
});

test("manga and manhwa queries send print filters and omit anime filters", () => {
  for (const contentType of ["MANGA", "MANHWA"]) {
    const filters = {
      ...filtersFor(),
      status: "PUBLISHING",
      min_chapters: "25",
      max_chapters: "200",
      min_volumes: "3",
      max_volumes: "20",
      min_episodes: "12",
      type: "TV",
      tag: ["school"],
      exclude_genre: ["Isekai"],
      exclude_tag: ["harem"],
      studio: ["Bones"],
      streaming_service: ["Crunchyroll"],
      author: ["Hiromu Arakawa"],
    };
    const params = new URLSearchParams(queryString(filters, 1, contentType));

    assert.equal(params.get("content_type"), contentType);
    assert.equal(params.get("status"), "PUBLISHING");
    assert.equal(params.get("min_chapters"), "25");
    assert.equal(params.get("max_chapters"), "200");
    assert.equal(params.get("min_volumes"), "3");
    assert.equal(params.get("max_volumes"), "20");
    assert.equal(params.get("tag"), "school");
    assert.equal(params.get("exclude_genre"), "Isekai");
    assert.equal(params.get("exclude_tag"), "harem");
    assert.equal(params.has("min_episodes"), false);
    assert.equal(params.has("type"), false);
    assert.equal(params.has("studio"), false);
    assert.equal(params.has("streaming_service"), false);
    assert.equal(params.get("author"), "Hiromu Arakawa");
  }
});

test("all-content queries include only shared filters", () => {
  const filters = {
    ...filtersFor(),
    q: "hero",
    min_score: "7",
    min_year: "2000",
    max_year: "2026",
    min_episodes: "6",
    max_episodes: "24",
    min_chapters: "10",
    max_chapters: "100",
    min_volumes: "2",
    max_volumes: "12",
    studio: ["Bones"],
    streaming_service: ["Netflix"],
    author: ["SIU"],
    type: "TV",
    status: "PUBLISHING",
    exclude_genre: ["Isekai"],
    exclude_tag: ["harem"],
  };
  const params = new URLSearchParams(queryString(filters, 1, "ALL"));

  assert.equal(params.get("q"), "hero");
  assert.equal(params.get("min_score"), "7");
  assert.equal(params.get("min_year"), "2000");
  assert.equal(params.get("max_year"), "2026");
  assert.equal(params.get("exclude_genre"), "Isekai");
  assert.equal(params.get("exclude_tag"), "harem");
  assert.equal(params.has("min_episodes"), false);
  assert.equal(params.has("max_episodes"), false);
  assert.equal(params.has("min_chapters"), false);
  assert.equal(params.has("max_chapters"), false);
  assert.equal(params.has("min_volumes"), false);
  assert.equal(params.has("max_volumes"), false);
  assert.equal(params.has("studio"), false);
  assert.equal(params.has("streaming_service"), false);
  assert.equal(params.has("author"), false);
  assert.equal(params.has("type"), false);
  assert.equal(params.has("status"), false);
});

test("random catalogue queries preserve compatible active filters", () => {
  const params = new URLSearchParams(randomQueryString({
    ...filtersFor(),
    min_score: "8",
    studio: ["Bones"],
    streaming_service: ["Crunchyroll"],
    exclude_genre: ["Isekai"],
  }, "ANIME", 6));

  assert.equal(params.get("content_type"), "ANIME");
  assert.equal(params.get("limit"), "6");
  assert.equal(params.get("preview"), "1");
  assert.equal(params.get("min_score"), "8");
  assert.equal(params.get("studio"), "Bones");
  assert.equal(params.get("streaming_service"), "Crunchyroll");
  assert.equal(params.get("exclude_genre"), "Isekai");
  assert.equal(params.has("page"), false);
  assert.equal(params.has("sort"), false);
});

test("cards use episodes for anime and chapters plus volumes for print media", () => {
  assert.deepEqual(
    itemMetadata({
      content_type: "ANIME",
      type: "TV",
      season: "spring",
      year: 2014,
      episodes: 24,
    }),
    ["TV", "Spring", 2014, "24 eps"],
  );
  assert.deepEqual(
    itemMetadata({
      content_type: "ANIME",
      type: "TV",
      status: "CURRENTLY_AIRING",
      season: "spring",
      year: 2026,
      episodes: 12,
    }),
    ["TV", "Spring", 2026, "12 eps"],
  );
  assert.deepEqual(
    itemMetadata({
      content_type: "ANIME",
      type: "TV",
      status: "CURRENTLY_AIRING",
      season: "spring",
      year: 2026,
      episodes: 12,
    }, true),
    ["TV", "Currently Airing", "Spring", 2026, "12 episodes"],
  );
  assert.deepEqual(
    itemMetadata({
      content_type: "MANHWA",
      status: "ON_HIATUS",
      publication_year: 2020,
      chapters: 50,
      volumes: 4,
    }),
    ["Manhwa", 2020, "50 ch", "4 vols"],
  );
  assert.equal(itemContentType({ content_type: "manga" }), "MANGA");
});

test("pagination keeps a stable eight-page window", () => {
  assert.deepEqual(visiblePageNumbers(1, 20), [1, 2, 3, 4, 5, 6, 7, 8]);
  assert.deepEqual(
    visiblePageNumbers(20, 20),
    [13, 14, 15, 16, 17, 18, 19, 20],
  );
});

test("sort options remain relevant to the selected content type", () => {
  for (const contentType of ["ANIME", "MANGA", "MANHWA", "ALL"]) {
    assert.equal(
      sortOptionsFor(contentType).some(({ value }) => value === "most_popular"),
      true,
    );
    assert.equal(
      sortOptionsFor(contentType).some(({ value }) => value === "most_members"),
      false,
    );
  }
  assert.equal(
    sortOptionsFor("ANIME").some(({ value }) => value === "most_episodes"),
    false,
  );
  assert.equal(
    sortOptionsFor("MANGA").some(({ value }) => value === "most_chapters"),
    false,
  );
  assert.equal(
    sortOptionsFor("MANHWA").some(({ value }) => value === "most_chapters"),
    false,
  );
  assert.equal(sortOptionsFor("ALL").some(({ value }) => value === "most_episodes"), false);
});

test("URL state round-trips filters, sorting, content type, and page", () => {
  const filters = {
    ...filtersFor(),
    q: "tower",
    min_score: "7.5",
    min_year: "2015",
    max_year: "2025",
    min_chapters: "20",
    max_chapters: "120",
    min_volumes: "2",
    max_volumes: "15",
    status: "PUBLISHING",
    genre: ["Action", "Fantasy"],
    tag: ["school"],
    exclude_genre: ["Isekai"],
    exclude_tag: ["harem"],
  };
  const search = catalogueUrlSearch({
    contentType: "MANHWA",
    filters,
    page: 4,
    sort: "most_popular",
    view: "results",
  });
  const restored = catalogueStateFromSearch(search);

  assert.equal(restored.contentType, "MANHWA");
  assert.equal(restored.page, 4);
  assert.equal(restored.sort, "most_popular");
  assert.equal(restored.filters.q, "tower");
  assert.equal(restored.filters.status, "PUBLISHING");
  assert.equal(restored.filters.max_year, "2025");
  assert.equal(restored.filters.min_chapters, "20");
  assert.equal(restored.filters.max_chapters, "120");
  assert.equal(restored.filters.min_volumes, "2");
  assert.equal(restored.filters.max_volumes, "15");
  assert.deepEqual(restored.filters.genre, ["Action", "Fantasy"]);
  assert.deepEqual(restored.filters.tag, ["school"]);
  assert.deepEqual(restored.filters.exclude_genre, ["Isekai"]);
  assert.deepEqual(restored.filters.exclude_tag, ["harem"]);
});

test("direct URL state safely normalizes invalid navigation values", () => {
  const restored = catalogueStateFromSearch(
    "?content_type=NOVEL&page=-2&sort=most_chapters",
  );

  assert.equal(restored.contentType, "ANIME");
  assert.equal(restored.page, 1);
  assert.equal(restored.sort, DEFAULT_SORT);
});

test("anime homepage URL state preserves the implicit TV query", () => {
  const search = catalogueUrlSearch({
    contentType: "ANIME",
    filters: filtersFor(),
    page: 3,
    sort: DEFAULT_SORT,
    view: "home",
  });
  const restored = catalogueStateFromSearch(search);

  assert.equal(restored.view, "home");
  assert.equal(restored.page, 3);
  assert.equal(restored.filters.type, "");
});

test("anime airing status survives shareable URL round-trips", () => {
  const filters = {
    ...filtersFor(),
    type: "TV",
    status: "CURRENTLY_AIRING",
  };
  const search = catalogueUrlSearch({
    contentType: "ANIME",
    filters,
    page: 2,
    view: "results",
  });
  const restored = catalogueStateFromSearch(search);

  assert.equal(restored.contentType, "ANIME");
  assert.equal(restored.page, 2);
  assert.equal(restored.filters.status, "CURRENTLY_AIRING");
  assert.equal(
    new URLSearchParams(search).get("status"),
    "CURRENTLY_AIRING",
  );
});

test("all-content URL state ignores media-specific filters", () => {
  const restored = catalogueStateFromSearch(
    "?content_type=ALL&q=hero&min_score=7&min_episodes=6&max_episodes=24"
    + "&min_chapters=10&max_chapters=100&min_volumes=1&max_volumes=12"
    + "&studio=Bones,Madhouse&streaming_service=Crunchyroll,Netflix"
    + "&author=SIU,Hiromu%20Arakawa",
  );

  assert.equal(restored.contentType, "ALL");
  assert.equal(restored.filters.q, "hero");
  assert.equal(restored.filters.min_score, "7");
  assert.equal(restored.filters.max_episodes, "");
  assert.equal(restored.filters.max_chapters, "");
  assert.equal(restored.filters.max_volumes, "");
  assert.deepEqual(restored.filters.studio, []);
  assert.deepEqual(restored.filters.streaming_service, []);
  assert.deepEqual(restored.filters.author, []);
});

test("presets add their filters without clearing existing selections", () => {
  const animePresets = presetsFor("ANIME", new Date("2026-07-15T12:00:00Z"));
  const seasonal = animePresets.find(({ id }) => id === "new-season");
  const shortSeries = animePresets.find(({ id }) => id === "short-series");
  const longSeries = animePresets.find(({ id }) => id === "long-series");
  const completedAnime = animePresets.find(({ id }) => id === "completed-anime");
  const completedManga = presetsFor("MANGA").find(
    ({ id }) => id === "completed-manga",
  );
  const completedManhwa = presetsFor("MANHWA").find(
    ({ id }) => id === "completed-manhwa",
  );

  assert.deepEqual(
    {
      type: filtersFromPreset(seasonal).type,
      status: filtersFromPreset(seasonal).status,
      season: filtersFromPreset(seasonal).season,
      min_year: filtersFromPreset(seasonal).min_year,
    },
    {
      type: "TV",
      status: "CURRENTLY_AIRING",
      season: "summer",
      min_year: "2026",
    },
  );
  assert.equal(filtersFromPreset(shortSeries).max_episodes, "13");
  assert.equal(filtersFromPreset(longSeries).min_episodes, "24");
  assert.equal(filtersFromPreset(completedAnime).status, "FINISHED_AIRING");
  assert.equal(
    hasConflictingRangeFilters(
      filtersFromPreset(longSeries, filtersFromPreset(shortSeries)),
    ),
    true,
  );
  assert.equal(hasConflictingRangeFilters(filtersFromPreset(shortSeries)), false);
  assert.equal(filtersFromPreset(completedManga).status, "FINISHED");
  assert.equal(filtersFromPreset(completedManga).genre.length, 0);
  assert.equal(filtersFromPreset(completedManhwa).status, "FINISHED");

  const stacked = filtersFromPreset(completedManga, {
    ...filtersFor("MANGA"),
    q: "berserk",
    genre: ["Action"],
    tag: ["gore"],
    exclude_tag: ["harem"],
    min_score: "8",
    status: "PUBLISHING",
  });
  assert.equal(stacked.status, "FINISHED");
  assert.equal(stacked.q, "berserk");
  assert.deepEqual(stacked.genre, ["Action"]);
  assert.deepEqual(stacked.tag, ["gore"]);
  assert.deepEqual(stacked.exclude_tag, ["harem"]);
  assert.equal(stacked.min_score, "8");
});

test("active chips represent each removable catalogue filter", () => {
  const chips = activeFilterChips({
    ...filtersFor(),
    type: "TV",
    season: "spring",
    status: "FINISHED_AIRING",
    min_score: "8",
    min_year: "2020",
    max_year: "2026",
    min_episodes: "12",
    max_episodes: "24",
    genre: ["Drama"],
    tag: ["school"],
    exclude_genre: ["Isekai"],
    exclude_tag: ["harem"],
    studio: ["Bones"],
    streaming_service: ["Crunchyroll"],
  }, "ANIME");

  assert.deepEqual(
    chips.map(({ key }) => key),
    [
      "genre",
      "tag",
      "exclude_genre",
      "exclude_tag",
      "studio",
      "streaming_service",
      "min_year:max_year",
      "min_episodes:max_episodes",
      "min_score",
      "type",
      "season",
      "status",
    ],
  );
});

test("print chips use bounded chapter and volume ranges without anime facets", () => {
  const chips = activeFilterChips({
    ...filtersFor(),
    min_chapters: "10",
    max_chapters: "100",
    min_volumes: "2",
    max_volumes: "12",
    studio: ["Bones"],
    streaming_service: ["Netflix"],
    author: ["Hiromu Arakawa"],
  }, "MANGA");

  assert.deepEqual(
    chips.map(({ label }) => label),
    [
      "Author: Hiromu Arakawa",
      "Chapters: 10–100",
      "Volumes: 2–12",
    ],
  );
});

test("removing a chip clears only its matching filter value", () => {
  const filters = {
    ...filtersFor(),
    type: "TV",
    genre: ["Action", "Drama"],
    tag: ["school"],
    exclude_genre: ["Isekai"],
    studio: ["Bones", "Madhouse"],
    min_year: "2000",
    max_year: "2020",
  };
  const withoutAction = filtersWithoutChip(
    filters,
    { key: "genre", value: "Action" },
  );
  const withoutType = filtersWithoutChip(
    withoutAction,
    { key: "type", value: "TV" },
  );
  const withoutStatus = filtersWithoutChip(
    { ...withoutAction, status: "CURRENTLY_AIRING" },
    { key: "status", value: "CURRENTLY_AIRING" },
  );
  const withoutStudio = filtersWithoutChip(
    filters,
    { key: "studio", value: "Bones" },
  );
  const withoutYear = filtersWithoutChip(
    filters,
    {
      key: "min_year:max_year",
      keys: ["min_year", "max_year"],
      value: "2000:2020",
    },
  );
  const withoutExcludedGenre = filtersWithoutChip(
    filters,
    { key: "exclude_genre", value: "Isekai" },
  );

  assert.deepEqual(withoutAction.genre, ["Drama"]);
  assert.deepEqual(withoutAction.tag, ["school"]);
  assert.equal(withoutAction.type, "TV");
  assert.equal(withoutType.type, "");
  assert.equal(withoutStatus.status, "");
  assert.deepEqual(withoutStudio.studio, ["Madhouse"]);
  assert.deepEqual(withoutExcludedGenre.exclude_genre, []);
  assert.equal(withoutYear.min_year, "");
  assert.equal(withoutYear.max_year, "");
  assert.deepEqual(filters.genre, ["Action", "Drama"]);
  assert.deepEqual(filters.studio, ["Bones", "Madhouse"]);
});

test("jump-to-page validation rejects unsafe and out-of-range pages", () => {
  assert.equal(validatedPage("4", 760), 4);
  assert.equal(validatedPage("0", 760), null);
  assert.equal(validatedPage("761", 760), null);
  assert.equal(validatedPage("4.5", 760), null);
  assert.equal(validatedPage("hello", 760), null);
});

test("responsive filter panel stays collapsed until explicitly opened", () => {
  const collapsed = responsiveFilterPanelClasses(false, false);
  const mobileOpen = responsiveFilterPanelClasses(true, false);
  const desktopOpen = responsiveFilterPanelClasses(false, true);

  assert.match(collapsed, /\bhidden\b/);
  assert.match(collapsed, /\bsm:hidden\b/);
  assert.match(mobileOpen, /\bgrid\b/);
  assert.match(desktopOpen, /\bsm:grid\b/);
});

test("range labels distinguish unrestricted, one-sided, and bounded filters", () => {
  assert.equal(rangeSelectionLabel("", ""), "Any");
  assert.equal(rangeSelectionLabel("2000", ""), "2000+");
  assert.equal(rangeSelectionLabel("", "2020"), "Up to 2020");
  assert.equal(rangeSelectionLabel("2000", "2020"), "2000–2020");
});

test("adaptive range scales keep common lengths dense and preserve exact URLs", () => {
  const episodes = discreteRangeValues(1, 3000, "episodes", [203]);
  const chapters = discreteRangeValues(1, 6477, "chapters");

  assert.equal(episodes.includes(12), true);
  assert.equal(episodes.includes(24), true);
  assert.equal(episodes.includes(203), true);
  assert.equal(episodes.includes(3000), true);
  assert.equal(episodes.length < 250, true);
  assert.equal(chapters.includes(100), true);
  assert.equal(chapters.includes(6477), true);
  assert.equal(chapters.length < 450, true);
  assert.equal(
    episodes[nearestRangeIndex(episodes, 24)],
    24,
  );
});

test("studio and streaming helpers normalize names and safe external links", () => {
  assert.deepEqual(
    namedValues([
      " Bones ",
      { name: "Madhouse" },
      { name: "bones" },
      null,
    ]),
    ["Bones", "Madhouse"],
  );
  assert.equal(safeExternalUrl("javascript:alert(1)"), null);
  assert.equal(safeExternalUrl("not a URL"), null);
  assert.equal(
    safeExternalUrl("https://www.crunchyroll.com/watch"),
    "https://www.crunchyroll.com/watch",
  );
  assert.deepEqual(
    streamingServiceEntries([
      { name: "Crunchyroll", url: "https://www.crunchyroll.com/watch" },
      { name: "crunchyroll", url: "https://duplicate.example" },
      { name: "Netflix", url: "javascript:alert(1)" },
    ]),
    [
      {
        name: "Crunchyroll",
        url: "https://www.crunchyroll.com/watch",
      },
      { name: "Netflix", url: null },
    ],
  );
  assert.equal(
    streamingServiceBrand("Crunchyroll", "https://www.crunchyroll.com/watch"),
    "crunchyroll",
  );
  assert.equal(
    streamingServiceBrand("Watch now", "https://www.netflix.com/title/123"),
    "netflix",
  );
  assert.equal(
    streamingServiceBrand("Amazon Prime Video"),
    "prime-video",
  );
  assert.equal(streamingServiceBrand("Unknown provider"), "external");
  assert.equal(
    serviceFaviconUrl("https://www.bilibili.tv/play/123"),
    "https://www.bilibili.tv/favicon.ico",
  );
  assert.equal(serviceFaviconUrl("javascript:alert(1)"), null);
});

test("freshness text is omitted for missing data and humanized when present", () => {
  const now = new Date("2026-07-29T16:00:00Z");

  assert.equal(formatFreshness(null, now), null);
  assert.equal(
    formatFreshness("2026-07-29T14:00:00Z", now),
    "Latest catalogue change 2 hours ago",
  );
  assert.equal(
    formatFreshness("2026-07-29T14:00:00Z", now, "Details checked with Jikan"),
    "Details checked with Jikan 2 hours ago",
  );
});
