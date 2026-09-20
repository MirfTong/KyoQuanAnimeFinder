export const CONTENT_TYPES = [
  { value: "ANIME", label: "Anime", resultLabel: "anime" },
  { value: "MANGA", label: "Manga", resultLabel: "manga" },
  { value: "MANHWA", label: "Manhwa", resultLabel: "manhwa" },
  { value: "ALL", label: "All", resultLabel: "titles" },
];

export const DEFAULT_SORT = "top_rated";

const EMPTY_FILTERS = {
  q: "",
  min_score: "",
  min_year: "",
  max_year: "",
  min_episodes: "",
  max_episodes: "",
  min_chapters: "",
  max_chapters: "",
  min_volumes: "",
  max_volumes: "",
  type: "",
  season: "",
  status: "",
  genre: [],
  tag: [],
  exclude_genre: [],
  exclude_tag: [],
  studio: [],
  streaming_service: [],
  author: [],
};

const COMMON_FILTER_KEYS = [
  "q",
  "min_score",
  "min_year",
  "max_year",
  "genre",
  "tag",
  "exclude_genre",
  "exclude_tag",
];
const ANIME_FILTER_KEYS = [
  ...COMMON_FILTER_KEYS,
  "min_episodes",
  "max_episodes",
  "type",
  "season",
  "status",
  "studio",
  "streaming_service",
];
const PRINT_FILTER_KEYS = [
  ...COMMON_FILTER_KEYS,
  "min_chapters",
  "max_chapters",
  "min_volumes",
  "max_volumes",
  "status",
  "author",
];
const ALL_FILTER_KEYS = [...COMMON_FILTER_KEYS];
const PAGE_WINDOW_SIZE = 8;
const RANGE_FILTER_PAIRS = [
  ["min_year", "max_year"],
  ["min_episodes", "max_episodes"],
  ["min_chapters", "max_chapters"],
  ["min_volumes", "max_volumes"],
];

export function filtersFor() {
  return {
    ...EMPTY_FILTERS,
    genre: [],
    tag: [],
    exclude_genre: [],
    exclude_tag: [],
    studio: [],
    streaming_service: [],
    author: [],
  };
}

function hasInvertedRange(filters, minimumKey, maximumKey) {
  const minimum = filters[minimumKey];
  const maximum = filters[maximumKey];
  if (minimum === "" || maximum === "") return false;
  const numericMinimum = Number(minimum);
  const numericMaximum = Number(maximum);
  return Number.isFinite(numericMinimum)
    && Number.isFinite(numericMaximum)
    && numericMinimum > numericMaximum;
}

export function hasConflictingRangeFilters(filters) {
  return RANGE_FILTER_PAIRS.some(([minimumKey, maximumKey]) => (
    hasInvertedRange(filters, minimumKey, maximumKey)
  ));
}

export const TOP_RATED_FILTERS = { ...filtersFor(), type: "TV" };

export function filtersMatch(left, right) {
  return Object.keys(EMPTY_FILTERS).every((key) => {
    if (!Array.isArray(left[key])) return left[key] === right[key];
    return left[key].length === right[key].length
      && left[key].every((value, index) => value === right[key][index]);
  });
}

export function scoreLabel(value) {
  if (value === null || value === undefined || value === "") return "?";
  const numericValue = Number(value);
  return Number.isFinite(numericValue) ? numericValue.toFixed(2) : "?";
}

export function usesTopRatedAnimeHomepage(
  contentType,
  filters,
  allTypesExplicitlySelected = false,
) {
  return contentType === "ANIME"
    && filtersMatch(filters, filtersFor())
    && !allTypesExplicitlySelected;
}

function filterKeysFor(contentType) {
  if (contentType === "ANIME") return ANIME_FILTER_KEYS;
  if (contentType === "MANGA" || contentType === "MANHWA") {
    return PRINT_FILTER_KEYS;
  }
  return ALL_FILTER_KEYS;
}

function addFilterParams(params, filters, contentType) {
  filterKeysFor(contentType).forEach((key) => {
    const value = filters[key];
    if (Array.isArray(value) ? value.length > 0 : value) {
      params.set(key, Array.isArray(value) ? value.join(",") : value);
    }
  });
}

export function queryString(
  filters,
  page = 1,
  contentType = "ANIME",
  sort = DEFAULT_SORT,
) {
  const params = new URLSearchParams({
    content_type: contentType,
    page: String(page),
    per_page: "24",
    sort,
    preview: "1",
  });
  addFilterParams(params, filters, contentType);
  return params.toString();
}

export function randomQueryString(
  filters,
  contentType = "ANIME",
  limit = 6,
) {
  const params = new URLSearchParams({
    content_type: contentType,
    limit: String(limit),
    preview: "1",
  });
  addFilterParams(params, filters, contentType);
  return params.toString();
}

export function catalogueUrlSearch({
  contentType = "ANIME",
  filters = filtersFor(),
  page = 1,
  sort = DEFAULT_SORT,
  view = "results",
} = {}) {
  const params = new URLSearchParams();
  if (contentType !== "ANIME") params.set("content_type", contentType);
  if (page > 1) params.set("page", String(page));
  if (sort !== DEFAULT_SORT) params.set("sort", sort);
  if (view === "home") {
    params.set("view", "home");
  } else {
    addFilterParams(params, filters, contentType);
  }
  const value = params.toString();
  return value ? `?${value}` : "";
}

function commaSeparatedValues(params, key) {
  return params
    .getAll(key)
    .flatMap((value) => value.split(","))
    .map((value) => value.trim())
    .filter(Boolean)
    .filter((value, index, values) => values.indexOf(value) === index);
}

export function catalogueStateFromSearch(search = "") {
  const params = new URLSearchParams(search);
  const requestedContentType = (params.get("content_type") ?? "ANIME").toUpperCase();
  const contentType = CONTENT_TYPES.some(({ value }) => value === requestedContentType)
    ? requestedContentType
    : "ANIME";
  const validSortValues = sortOptionsFor(contentType).map(({ value }) => value);
  const requestedSort = params.get("sort") ?? DEFAULT_SORT;
  const sort = validSortValues.includes(requestedSort) ? requestedSort : DEFAULT_SORT;
  const requestedPage = Number(params.get("page") ?? 1);
  const page = Number.isInteger(requestedPage) && requestedPage > 0
    ? requestedPage
    : 1;
  const view = params.get("view") === "home" && contentType === "ANIME"
    ? "home"
    : "results";
  const filters = filtersFor();
  if (view !== "home") {
    filterKeysFor(contentType).forEach((key) => {
      filters[key] = Array.isArray(filters[key])
        ? commaSeparatedValues(params, key)
        : params.get(key) ?? "";
    });
  }
  const hasState = [...params.keys()].some((key) => (
    [
      "content_type",
      "page",
      "sort",
      "view",
      ...filterKeysFor(contentType),
    ].includes(key)
  ));
  return {
    contentType,
    filters,
    page,
    sort,
    view: hasState ? view : "home",
    hasState,
  };
}

export function contentTypeDetails(value) {
  return CONTENT_TYPES.find((contentType) => contentType.value === value)
    ?? CONTENT_TYPES[0];
}

export function itemContentType(item) {
  const value = item?.content_type?.toUpperCase();
  return CONTENT_TYPES.some(
    (contentType) => contentType.value === value && value !== "ALL",
  )
    ? value
    : "ANIME";
}

function formatSeason(season) {
  if (!season) return null;
  return `${season.charAt(0).toUpperCase()}${season.slice(1)}`;
}

function formatStatus(status) {
  if (!status) return null;
  return status
    .replaceAll("_", " ")
    .toLowerCase()
    .replace(/\b\w/g, (character) => character.toUpperCase());
}

export function itemMetadata(item, detailed = false) {
  const contentType = itemContentType(item);
  const year = item.year ?? item.publication_year ?? "Unknown year";

  if (contentType === "ANIME") {
    return [
      item.type || "Anime",
      detailed ? formatStatus(item.status) : null,
      formatSeason(item.season),
      year,
      `${item.episodes ?? "?"} ${detailed ? "episodes" : "eps"}`,
    ].filter(Boolean);
  }

  return [
    contentTypeDetails(contentType).label,
    detailed ? formatStatus(item.status) : null,
    year,
    `${item.chapters ?? "?"} ${detailed ? "chapters" : "ch"}`,
    `${item.volumes ?? "?"} ${detailed ? "volumes" : "vols"}`,
  ].filter(Boolean);
}

export function sortOptionsFor(contentType) {
  const options = [
    { value: "top_rated", label: "Top rated" },
    { value: "most_popular", label: "Most popular" },
    { value: "newest", label: "Newest release" },
    { value: "oldest", label: "Oldest release" },
    { value: "title", label: "A–Z" },
  ];
  return options;
}

function currentSeason(now) {
  const seasons = ["winter", "spring", "summer", "fall"];
  return seasons[Math.floor(now.getMonth() / 3)];
}

export function presetsFor(contentType, now = new Date()) {
  const presets = [];
  if (contentType === "ANIME") {
    presets.push(
      {
        id: "new-season",
        label: "New this season",
        filters: {
          type: "TV",
          status: "CURRENTLY_AIRING",
          season: currentSeason(now),
          min_year: String(now.getFullYear()),
          max_year: String(now.getFullYear()),
        },
      },
      {
        id: "short-series",
        label: "Short series",
        filters: { type: "TV", max_episodes: "13" },
      },
      {
        id: "long-series",
        label: "Long series",
        filters: { type: "TV", min_episodes: "24" },
      },
      {
        id: "movies",
        label: "Movies",
        filters: { type: "MOVIE" },
      },
      {
        id: "completed-anime",
        label: "Completed anime",
        filters: { status: "FINISHED_AIRING" },
      },
    );
  }
  presets.push({
    id: "highly-rated",
    label: "Highly rated",
    filters: { min_score: "8" },
  });
  if (contentType === "MANGA") {
    presets.push({
      id: "completed-manga",
      label: "Completed manga",
      filters: { status: "FINISHED" },
    });
  }
  if (contentType === "MANHWA") {
    presets.push({
      id: "completed-manhwa",
      label: "Completed manhwa",
      filters: { status: "FINISHED" },
    });
  }
  return presets;
}

export function filtersFromPreset(preset, existingFilters = filtersFor()) {
  return {
    ...filtersFor(),
    ...existingFilters,
    ...preset.filters,
  };
}

export function activeFilterChips(filters, contentType) {
  const chips = [];
  const activeKeys = filterKeysFor(contentType);
  filters.genre.forEach((value) => {
    chips.push({ key: "genre", value, label: value });
  });
  filters.tag.forEach((value) => {
    chips.push({ key: "tag", value, label: `Tag: ${value}` });
  });
  filters.exclude_genre.forEach((value) => {
    chips.push({
      key: "exclude_genre",
      value,
      label: `Exclude genre: ${value}`,
    });
  });
  filters.exclude_tag.forEach((value) => {
    chips.push({
      key: "exclude_tag",
      value,
      label: `Exclude tag: ${value}`,
    });
  });
  if (activeKeys.includes("studio")) {
    filters.studio.forEach((value) => {
      chips.push({ key: "studio", value, label: `Studio: ${value}` });
    });
  }
  if (activeKeys.includes("streaming_service")) {
    filters.streaming_service.forEach((value) => {
      chips.push({
        key: "streaming_service",
        value,
        label: `Streaming: ${value}`,
      });
    });
  }
  if (activeKeys.includes("author")) {
    filters.author.forEach((value) => {
      chips.push({ key: "author", value, label: `Author: ${value}` });
    });
  }

  const addRangeChip = (label, minKey, maxKey) => {
    const minimum = filters[minKey];
    const maximum = filters[maxKey];
    if (!activeKeys.includes(minKey)
      || (!minimum && !maximum)) {
      return;
    }
    let valueLabel;
    if (minimum && maximum) {
      valueLabel = `${minimum}–${maximum}`;
    } else if (minimum) {
      valueLabel = `${minimum}+`;
    } else {
      valueLabel = `up to ${maximum}`;
    }
    chips.push({
      key: `${minKey}:${maxKey}`,
      keys: [minKey, maxKey],
      value: `${minimum}:${maximum}`,
      label: `${label}: ${valueLabel}`,
    });
  };

  const scalarLabels = {
    type: (value) => `Type: ${value}`,
    season: (value) => `Season: ${formatSeason(value)}`,
    min_score: (value) => `Score: ${value}+`,
    status: (value) => `Status: ${formatStatus(value)}`,
  };

  addRangeChip("Year", "min_year", "max_year");
  addRangeChip("Episodes", "min_episodes", "max_episodes");
  addRangeChip("Chapters", "min_chapters", "max_chapters");
  addRangeChip("Volumes", "min_volumes", "max_volumes");

  activeKeys.forEach((key) => {
    if (
      key !== "q"
      && !Array.isArray(filters[key])
      && filters[key]
      && scalarLabels[key]
    ) {
      chips.push({ key, value: filters[key], label: scalarLabels[key](filters[key]) });
    }
  });
  return chips;
}

export function filtersWithoutChip(filters, chip) {
  const nextFilters = Object.fromEntries(
    Object.entries(filters).map(([key, value]) => [
      key,
      Array.isArray(value) ? [...value] : value,
    ]),
  );
  if (chip.keys) {
    chip.keys.forEach((key) => {
      if (Object.hasOwn(nextFilters, key)) nextFilters[key] = "";
    });
    return nextFilters;
  }
  if (Array.isArray(nextFilters[chip.key])) {
    nextFilters[chip.key] = nextFilters[chip.key].filter(
      (value) => value !== chip.value,
    );
  } else if (Object.hasOwn(nextFilters, chip.key)) {
    nextFilters[chip.key] = "";
  }
  return nextFilters;
}

export function rangeSelectionLabel(minimum, maximum) {
  if (!minimum && !maximum) return "Any";
  if (minimum && maximum) return `${minimum}–${maximum}`;
  return minimum ? `${minimum}+` : `Up to ${maximum}`;
}

export function discreteRangeValues(
  minimum,
  maximum,
  scale = "linear",
  includedValues = [],
) {
  const floor = Math.ceil(Number(minimum));
  const ceiling = Math.floor(Number(maximum));
  if (!Number.isFinite(floor) || !Number.isFinite(ceiling) || ceiling < floor) {
    return [0];
  }

  const values = [];
  let current = floor;
  while (current <= ceiling) {
    values.push(current);
    if (scale === "episodes") {
      current += current < 100 ? 1 : current < 500 ? 20 : 100;
    } else if (scale === "chapters") {
      current += current < 200 ? 1 : current < 1000 ? 20 : 200;
    } else if (scale === "volumes") {
      current += current < 100 ? 1 : current < 500 ? 10 : 50;
    } else {
      current += 1;
    }
  }

  values.push(
    ceiling,
    ...includedValues
      .map(Number)
      .filter((value) => Number.isFinite(value) && value >= floor && value <= ceiling),
  );
  return [...new Set(values)].sort((left, right) => left - right);
}

export function nearestRangeIndex(values, requestedValue) {
  const target = Number(requestedValue);
  if (!Number.isFinite(target) || values.length === 0) return 0;
  let low = 0;
  let high = values.length - 1;
  while (low < high) {
    const middle = Math.floor((low + high) / 2);
    if (values[middle] < target) low = middle + 1;
    else high = middle;
  }
  if (low === 0) return 0;
  const previous = low - 1;
  return Math.abs(values[low] - target) < Math.abs(values[previous] - target)
    ? low
    : previous;
}

export function namedValues(entries) {
  const seen = new Set();
  return (entries ?? [])
    .map((entry) => (
      typeof entry === "string"
        ? entry.trim()
        : String(entry?.name ?? entry?.title ?? "").trim()
    ))
    .filter((name) => {
      if (!name) return false;
      const key = name.toLocaleLowerCase();
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
}

export function safeExternalUrl(value) {
  if (!value) return null;
  try {
    const parsed = new URL(value);
    return ["http:", "https:"].includes(parsed.protocol) ? parsed.href : null;
  } catch {
    return null;
  }
}

export function streamingServiceEntries(entries) {
  const seen = new Set();
  return (entries ?? [])
    .map((entry) => {
      const name = typeof entry === "string"
        ? entry.trim()
        : String(entry?.name ?? entry?.title ?? "").trim();
      const url = typeof entry === "string"
        ? null
        : safeExternalUrl(entry?.url);
      return { name, url };
    })
    .filter(({ name }) => {
      if (!name) return false;
      const key = name.toLocaleLowerCase();
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
}

export function streamingServiceBrand(name, url = "") {
  const identity = `${name ?? ""} ${url ?? ""}`.toLocaleLowerCase();
  const brands = [
    ["myanimelist", ["myanimelist", "my anime list"]],
    ["crunchyroll", ["crunchyroll"]],
    ["netflix", ["netflix"]],
    ["hulu", ["hulu"]],
    ["prime-video", ["prime video", "primevideo", "amazon.com/gp/video"]],
    ["hotstar", ["hotstar"]],
    ["disney-plus", ["disney+", "disney plus", "disneyplus"]],
    ["hidive", ["hidive"]],
    ["funimation", ["funimation"]],
    ["youtube", ["youtube", "youtu.be"]],
    ["tubi", ["tubi"]],
    ["peacock", ["peacocktv", "peacock tv"]],
    ["retrocrush", ["retrocrush"]],
    ["max", ["hbo max", "hbomax", "max.com"]],
    ["apple-tv", ["apple tv", "tv.apple.com"]],
  ];
  return brands.find(([, aliases]) => (
    aliases.some((alias) => identity.includes(alias))
  ))?.[0] ?? "external";
}

export function serviceFaviconUrl(url) {
  const safeUrl = safeExternalUrl(url);
  if (!safeUrl) return null;
  const parsed = new URL(safeUrl);
  return `${parsed.origin}/favicon.ico`;
}

export function validatedPage(value, totalPages) {
  const page = Number(value);
  return Number.isInteger(page) && page >= 1 && page <= totalPages
    ? page
    : null;
}

export function responsiveFilterPanelClasses(
  mobileOpen,
  moreOpen,
  desktopColumnSpan = "lg:col-span-6",
  desktopGridColumns = "lg:grid-cols-6",
) {
  return [
    mobileOpen ? "grid" : "hidden",
    moreOpen ? "sm:grid" : "sm:hidden",
    "gap-3 sm:col-span-2 sm:grid-cols-2",
    desktopColumnSpan,
    desktopGridColumns,
  ].join(" ");
}

export function formatFreshness(
  timestamp,
  now = new Date(),
  label = "Latest catalogue change",
) {
  if (!timestamp) return null;
  const updated = new Date(timestamp);
  if (Number.isNaN(updated.getTime())) return null;
  const elapsedMinutes = Math.max(
    0,
    Math.floor((now.getTime() - updated.getTime()) / 60_000),
  );
  if (elapsedMinutes < 1) return `${label} just now`;
  if (elapsedMinutes < 60) {
    return `${label} ${elapsedMinutes} minute${elapsedMinutes === 1 ? "" : "s"} ago`;
  }
  const elapsedHours = Math.floor(elapsedMinutes / 60);
  if (elapsedHours < 24) {
    return `${label} ${elapsedHours} hour${elapsedHours === 1 ? "" : "s"} ago`;
  }
  const elapsedDays = Math.floor(elapsedHours / 24);
  return `${label} ${elapsedDays} day${elapsedDays === 1 ? "" : "s"} ago`;
}

export function visiblePageNumbers(currentPage, totalPages) {
  const windowSize = Math.min(PAGE_WINDOW_SIZE, totalPages);
  const halfWindow = Math.floor(windowSize / 2);
  const firstPage = Math.max(
    1,
    Math.min(currentPage - halfWindow, totalPages - windowSize + 1),
  );

  return Array.from({ length: windowSize }, (_, index) => firstPage + index);
}
