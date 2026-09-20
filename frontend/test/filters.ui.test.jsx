import { useRef, useState } from "react";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import App, {
  CoverImage,
  DualRangeSlider,
  memberCountLabel,
  MinimumSlider,
  popularityLabel,
  SearchableMultiSelect,
  ServiceBrandIcon,
} from "../src/App.jsx";
import { clearGetCache, getJson } from "../src/api.js";


function PickerHarness({
  options = ["Bones", "Madhouse", "MAPPA"],
  continuousScroll = false,
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState([]);
  const dropdownRef = useRef(null);
  return (
    <SearchableMultiSelect
      label="Studio"
      selected={selected}
      options={options}
      query={query}
      loading={false}
      continuousScroll={continuousScroll}
      open={open}
      dropdownRef={dropdownRef}
      onOpenChange={setOpen}
      onQueryChange={setQuery}
      onToggle={(value) => {
        setSelected((current) => (
          current.includes(value)
            ? current.filter((entry) => entry !== value)
            : [...current, value]
        ));
      }}
    />
  );
}

function DualSliderHarness({ initialMinimum = "", initialMaximum = "" }) {
  const [minimum, setMinimum] = useState(initialMinimum);
  const [maximum, setMaximum] = useState(initialMaximum);
  const update = (name, value) => {
    if (name === "min_episodes") setMinimum(value);
    else setMaximum(value);
  };
  return (
    <DualRangeSlider
      label="Episodes"
      minName="min_episodes"
      maxName="max_episodes"
      minValue={minimum}
      maxValue={maximum}
      bounds={{ min: 1, max: 3000, step: 1 }}
      scale="episodes"
      onValueChange={update}
    />
  );
}

function MinimumSliderHarness() {
  const [score, setScore] = useState("");
  return (
    <MinimumSlider
      label="Score"
      name="min_score"
      value={score}
      bounds={{ min: 0, max: 10, step: 0.1 }}
      onValueChange={(_name, value) => setScore(value)}
    />
  );
}

test("detail metrics format provider values and unknown values safely", () => {
  expect(popularityLabel(245)).toBe("#245");
  expect(popularityLabel(null)).toBe("Not available");
  expect(memberCountLabel(1200000)).toMatch(/1\.2.?M/i);
  expect(memberCountLabel(undefined)).toBe("Not available");
});

function mockCatalogueFetch() {
  return vi.fn(async (input) => {
    const url = String(input);
    if (url.includes("/api/v1/anime/seasonal")) {
      return {
        ok: true,
        json: async () => ({
          items: [],
          pagination: { page: 1, pages: 1, total: 0 },
        }),
      };
    }
    if (url.includes("/api/v1/filter-ranges")) {
      return {
        ok: true,
        json: async () => ({
          ranges: {
            year: { min: 1917, max: 2027 },
            score: { min: 1.89, max: 9.46 },
            episodes: { min: 1, max: 3000 },
            chapters: { min: 1, max: 6477 },
            volumes: { min: 1, max: 200 },
          },
        }),
      };
    }
    if (
      url.includes("/api/v1/genres")
      || url.includes("/api/v1/studios")
      || url.includes("/api/v1/streaming-services")
      || url.includes("/api/v1/authors")
      || url.includes("/api/v1/tags")
    ) {
      return { ok: true, json: async () => ({ items: [] }) };
    }
    return {
      ok: true,
      json: async () => ({
        items: [],
        pagination: { page: 1, pages: 1, total: 0 },
        updated_at: null,
      }),
    };
  });
}

describe("rendered filter controls", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  test("searchable multi-select supports arrows, Enter, Escape, and focus restoration", async () => {
    const user = userEvent.setup();
    render(<PickerHarness />);

    const trigger = screen.getByRole("button", { name: "Studio" });
    await user.click(trigger);
    const search = screen.getByRole("combobox", { name: "Search studio" });
    expect(search).toHaveFocus();

    await user.keyboard("{ArrowDown}{Enter}");
    expect(
      screen.getByRole("option", { name: "Madhouse" }),
    ).toHaveAttribute("aria-selected", "true");

    await user.keyboard("{Escape}");
    await waitFor(() => expect(
      screen.getByRole("button", { name: "Studio (1)" }),
    ).toHaveFocus());
  });

  test("large searchable lists keep the rendered option count bounded", async () => {
    const user = userEvent.setup();
    const options = Array.from({ length: 500 }, (_, index) => (
      `Studio ${String(index).padStart(3, "0")}`
    ));
    render(<PickerHarness options={options} />);

    await user.click(screen.getByRole("button", { name: "Studio" }));
    expect(screen.getAllByRole("option")).toHaveLength(100);
    const moreOptions = screen.getByRole("button", { name: "More options" });
    expect(moreOptions).toBeInTheDocument();
    moreOptions.focus();
    await user.keyboard("{Enter}");
    expect(screen.getByRole("option", { name: "Studio 100" })).toBeInTheDocument();
    expect(screen.getAllByRole("option")).toHaveLength(100);
    expect(screen.getByRole("button", { name: "Previous options" })).toBeInTheDocument();

    await user.type(screen.getByRole("combobox", { name: "Search studio" }), "499");
    expect(screen.getByRole("option", { name: "Studio 499" })).toBeInTheDocument();
  });

  test("studio lists can continuously display options without navigation buttons", async () => {
    const user = userEvent.setup();
    const options = Array.from({ length: 150 }, (_, index) => `Studio ${index}`);
    render(<PickerHarness options={options} continuousScroll />);

    await user.click(screen.getByRole("button", { name: "Studio" }));
    expect(screen.getAllByRole("option")).toHaveLength(150);
    expect(screen.queryByRole("button", { name: "Previous options" })).toBeNull();
    expect(screen.queryByRole("button", { name: "More options" })).toBeNull();
  });

  test("cover images reserve a fixed ratio and fall back after an image error", () => {
    render(
      <div className="aspect-[2/3]">
        <CoverImage item={{ title: "Missing Cover", image_url: "/missing.jpg" }} />
      </div>,
    );
    const image = screen.getByRole("img", { name: "Missing Cover cover" });

    expect(image).toHaveAttribute("width", "400");
    expect(image).toHaveAttribute("height", "600");
    expect(image).toHaveAttribute("decoding", "async");
    fireEvent.error(image);
    expect(image).toHaveAttribute("src", "/cover-placeholder.svg");
  });

  test("streaming and MyAnimeList links use provider-specific brand icons", () => {
    const { container } = render(
      <div>
        <ServiceBrandIcon name="Netflix" />
        <ServiceBrandIcon name="Crunchyroll" />
        <ServiceBrandIcon name="MyAnimeList" brand="mal" />
        <ServiceBrandIcon
          name="Bilibili Global"
          url="https://www.bilibili.tv/play/123"
        />
      </div>,
    );

    expect(container.querySelector('[data-service-brand="netflix"]')).not.toBeNull();
    expect(container.querySelector('[data-service-brand="crunchyroll"]')).not.toBeNull();
    expect(container.querySelector('[data-service-brand="mal"]')).not.toBeNull();
    const providerIcon = container.querySelector('[data-service-brand="external"]');
    const favicon = providerIcon.querySelector("img");
    expect(favicon).toHaveAttribute("src", "https://www.bilibili.tv/favicon.ico");
    fireEvent.error(favicon);
    expect(providerIcon).toHaveTextContent("BG");
  });

  test("the entire dual-slider track accepts pointer input", () => {
    render(<DualSliderHarness />);
    const track = screen.getByTestId("min_episodes-max_episodes-track");
    vi.spyOn(track, "getBoundingClientRect").mockReturnValue({
      left: 0,
      right: 300,
      top: 0,
      bottom: 44,
      width: 300,
      height: 44,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    });

    fireEvent.pointerDown(track, { clientX: 90, pointerId: 1 });

    expect(screen.queryByText("Any")).not.toBeInTheDocument();
    expect(screen.getByText(/\+$/)).toBeInTheDocument();
  });

  test("a pointer can separate coincident dual-slider handles", () => {
    render(<DualSliderHarness initialMinimum="24" initialMaximum="24" />);
    const track = screen.getByTestId("min_episodes-max_episodes-track");
    vi.spyOn(track, "getBoundingClientRect").mockReturnValue({
      left: 0,
      right: 300,
      top: 0,
      bottom: 44,
      width: 300,
      height: 44,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    });
    const maximum = screen.getByRole("slider", {
      name: "Maximum episodes",
    });
    const coincidentPosition = (
      Number(maximum.value) / Number(maximum.max)
    ) * 300;

    fireEvent.pointerDown(track, {
      clientX: coincidentPosition + 40,
      pointerId: 2,
    });

    expect(Number(maximum.getAttribute("aria-valuenow"))).toBeGreaterThan(24);
  });

  test("score uses a clean clearable 0–10 scale", async () => {
    const user = userEvent.setup();
    render(<MinimumSliderHarness />);
    const score = screen.getByRole("slider", { name: "Minimum score" });

    expect(score).toHaveAttribute("min", "0");
    expect(score).toHaveAttribute("max", "10");
    expect(score).toHaveAttribute("step", "0.1");
    expect(screen.getByText("Any")).toBeInTheDocument();

    fireEvent.change(score, { target: { value: "7" } });
    expect(screen.getByText("7+")).toBeInTheDocument();
    expect(screen.getByTestId("min_score-progress")).toHaveStyle({
      width: "70%",
    });
    await user.click(screen.getByRole("button", { name: "Clear score" }));
    expect(screen.getByText("Any")).toBeInTheDocument();
  });
});

describe("browser response cache", () => {
  afterEach(() => {
    clearGetCache();
    vi.unstubAllGlobals();
  });

  test("reuses a recent successful GET response", async () => {
    const fetchResponse = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({ items: ["Action"] }),
    }));
    vi.stubGlobal("fetch", fetchResponse);

    const first = await getJson("/api/v1/genres?content_type=ANIME", {
      ttlMs: 60_000,
    });
    const second = await getJson("/api/v1/genres?content_type=ANIME", {
      ttlMs: 60_000,
    });

    expect(first.cached).toBe(false);
    expect(second.cached).toBe(true);
    expect(second.body.items).toEqual(["Action"]);
    expect(fetchResponse).toHaveBeenCalledTimes(1);
  });

  test("shares an in-flight cacheable detail request", async () => {
    let resolveResponse;
    const fetchResponse = vi.fn(() => new Promise((resolve) => {
      resolveResponse = () => resolve({
        ok: true,
        status: 200,
        json: async () => ({ item: { mal_id: 1 } }),
      });
    }));
    vi.stubGlobal("fetch", fetchResponse);

    const first = getJson("/api/v1/catalogue/ANIME/1", { ttlMs: 60_000 });
    const second = getJson("/api/v1/catalogue/ANIME/1", { ttlMs: 60_000 });

    expect(fetchResponse).toHaveBeenCalledTimes(1);
    resolveResponse();
    await expect(first).resolves.toMatchObject({ body: { item: { mal_id: 1 } } });
    await expect(second).resolves.toMatchObject({ body: { item: { mal_id: 1 } } });
  });

  test("turns a non-JSON provider outage into a useful API error", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({
      ok: false,
      status: 503,
      json: async () => { throw new SyntaxError("HTML response"); },
    })));

    const result = await getJson("/api/v1/catalogue", { cache: false });

    expect(result.ok).toBe(false);
    expect(result.body.error.message).toMatch(/temporarily unavailable/i);
  });
});

describe("catalogue filter integration", () => {
  let fetchMock;

  beforeEach(() => {
    clearGetCache();
    fetchMock = mockCatalogueFetch();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    window.history.replaceState({}, "", "/");
  });

  test("clearing a filter automatically clears its applied result state", async () => {
    const user = userEvent.setup();
    window.history.replaceState(
      {},
      "",
      "/?content_type=MANGA&min_score=8",
    );
    render(<App />);

    expect(await screen.findByText("Score: 8+")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Clear score" }));

    expect(screen.getByRole("button", {
      name: "Clear selections",
    })).toBeDisabled();
  });

  test("Season replaces Search and filter changes load results automatically", async () => {
    const user = userEvent.setup();
    render(<App />);

    expect(screen.queryByRole("button", { name: "Search" })).toBeNull();
    const season = screen.getByRole("combobox", { name: "Season" });
    await user.selectOptions(season, "summer");

    await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => {
      const request = new URL(String(input), "https://kyoquan.test");
      return request.pathname === "/api/v1/catalogue"
        && request.searchParams.get("season") === "summer";
    })).toBe(true));

    const search = screen.getByRole("textbox", { name: "Search catalogue" });
    await user.type(search, "Frieren");
    await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => {
      const request = new URL(String(input), "https://kyoquan.test");
      return request.pathname === "/api/v1/catalogue"
        && request.searchParams.get("q") === "Frieren";
    })).toBe(true));
  });

  test("Quick Picks disable presets that conflict with the active range", async () => {
    const user = userEvent.setup();
    render(<App />);

    await user.click(screen.getByRole("button", { name: "Short series" }));
    const longSeries = screen.getByRole("button", { name: "Long series" });
    expect(longSeries).toBeDisabled();
    expect(longSeries).toHaveAttribute(
      "title",
      "Conflicts with your current range filters",
    );
  });

  test("sorting the anime homepage retains the seasonal section and updates its results heading", async () => {
    const user = userEvent.setup();
    render(<App />);

    expect(await screen.findByRole("heading", {
      name: "POPULAR THIS SEASON",
    })).toBeInTheDocument();
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Sort catalogue" }),
      "most_popular",
    );

    expect(screen.getByRole("heading", {
      name: "POPULAR THIS SEASON",
    })).toBeInTheDocument();
    expect(screen.getByRole("heading", {
      name: "MOST POPULAR",
    })).toBeInTheDocument();
    await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => {
      const request = new URL(String(input), "https://kyoquan.test");
      return request.pathname === "/api/v1/catalogue"
        && request.searchParams.get("sort") === "most_popular"
        && request.searchParams.get("type") === "TV";
    })).toBe(true));
    expect(new URLSearchParams(window.location.search).get("view")).toBe("home");
  });

  test("the anime homepage loads a popular upcoming-season carousel", async () => {
    render(<App />);

    expect(await screen.findByRole("heading", {
      name: "UPCOMING NEXT SEASON",
    })).toBeInTheDocument();
    await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => {
      const request = new URL(String(input), "https://kyoquan.test");
      return request.pathname === "/api/v1/anime/seasonal"
        && request.searchParams.get("period") === "next"
        && request.searchParams.get("sort") === "most_popular"
        && request.searchParams.get("limit") === "6";
    })).toBe(true));
  });

  test("print-only entry points skip seasonal anime and bound facet responses", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/?content_type=MANGA");
    render(<App />);

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(fetchMock.mock.calls.some(([input]) => (
      String(input).includes("/api/v1/anime/seasonal")
    ))).toBe(false);
    expect(fetchMock.mock.calls.some(([input]) => (
      String(input).includes("/api/v1/authors")
    ))).toBe(false);
    await user.click(screen.getByRole("button", { name: "Author" }));
    let authorRequest;
    await waitFor(() => {
      authorRequest = fetchMock.mock.calls.find(([input]) => (
        String(input).includes("/api/v1/authors")
      ));
      expect(authorRequest).toBeDefined();
    });
    expect(authorRequest).toBeDefined();
    expect(
      new URL(String(authorRequest[0]), "https://kyoquan.test").searchParams.get("limit"),
    ).toBe("100");
  });

  test("author dropdown incrementally loads the next server page on scroll", async () => {
    const user = userEvent.setup();
    fetchMock.mockImplementation(async (input) => {
      const request = new URL(String(input), "https://kyoquan.test");
      if (request.pathname === "/api/v1/authors") {
        const offset = Number(request.searchParams.get("offset"));
        return {
          ok: true,
          json: async () => ({
            items: offset === 0
              ? Array.from({ length: 100 }, (_, index) => `Author ${index}`)
              : ["Author 100"],
            pagination: { has_more: offset === 0 },
          }),
        };
      }
      return mockCatalogueFetch()(input);
    });
    window.history.replaceState({}, "", "/?content_type=MANGA");
    render(<App />);

    await user.click(screen.getByRole("button", { name: "Author" }));
    expect(await screen.findByRole("option", { name: "Author 0" })).toBeInTheDocument();
    const listbox = screen.getByRole("listbox", { name: "Author" });
    Object.defineProperties(listbox, {
      scrollHeight: { configurable: true, value: 1000 },
      clientHeight: { configurable: true, value: 280 },
      scrollTop: { configurable: true, value: 720, writable: true },
    });
    fireEvent.scroll(listbox);

    expect(await screen.findByRole("option", { name: "Author 100" })).toBeInTheDocument();
    expect(listbox.scrollTop).toBe(0);
    expect(screen.getAllByRole("option").length).toBeLessThanOrEqual(100);
    expect(fetchMock.mock.calls.some(([input]) => {
      const request = new URL(String(input), "https://kyoquan.test");
      return request.pathname === "/api/v1/authors"
        && request.searchParams.get("offset") === "100";
    })).toBe(true);
  });

  test("genre and tag dropdown incrementally loads more tags on scroll", async () => {
    const user = userEvent.setup();
    fetchMock.mockImplementation(async (input) => {
      const request = new URL(String(input), "https://kyoquan.test");
      if (request.pathname === "/api/v1/tags") {
        const offset = Number(request.searchParams.get("offset"));
        return {
          ok: true,
          json: async () => ({
            items: offset === 0
              ? Array.from({ length: 100 }, (_, index) => `tag ${index}`)
              : ["tag 100"],
            pagination: { has_more: offset === 0 },
          }),
        };
      }
      return mockCatalogueFetch()(input);
    });
    render(<App />);

    await user.click(screen.getByText("Genres & Tags"));
    expect(await screen.findByRole("button", { name: "Include tag 0" })).toBeInTheDocument();
    const optionPanel = screen.getByLabelText("Genre and tag options");
    Object.defineProperties(optionPanel, {
      scrollHeight: { configurable: true, value: 1000 },
      clientHeight: { configurable: true, value: 280 },
      scrollTop: { configurable: true, value: 720, writable: true },
    });
    fireEvent.scroll(optionPanel);

    await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => {
      const request = new URL(String(input), "https://kyoquan.test");
      return request.pathname === "/api/v1/tags"
        && request.searchParams.get("offset") === "100";
    })).toBe(true));
    expect(await screen.findByRole("button", { name: "Include tag 100" })).toBeInTheDocument();
    expect(optionPanel.scrollTop).toBe(720);
    expect(screen.queryByText("Previous tags")).not.toBeInTheDocument();
    expect(screen.queryByText("More tags")).not.toBeInTheDocument();
  });

  test("genre and tag exclusions update results, URL state, and selection mode", async () => {
    const user = userEvent.setup();
    fetchMock.mockImplementation(async (input) => {
      const request = new URL(String(input), "https://kyoquan.test");
      if (request.pathname === "/api/v1/genres") {
        return { ok: true, json: async () => ({ items: ["Action", "Isekai"] }) };
      }
      if (request.pathname === "/api/v1/tags") {
        return {
          ok: true,
          json: async () => ({
            items: ["harem"],
            pagination: { has_more: false },
          }),
        };
      }
      return mockCatalogueFetch()(input);
    });
    render(<App />);

    await user.click(screen.getByText("Genres & Tags"));
    await screen.findByRole("button", { name: "Include Isekai" });
    await user.click(screen.getByRole("button", { name: "Exclude", exact: true }));
    const excludedGenre = screen.getByRole("button", { name: "Exclude Isekai" });
    await user.click(excludedGenre);

    expect(await screen.findByText("Exclude genre: Isekai")).toBeInTheDocument();
    expect(excludedGenre).toHaveClass("bg-rose-500/25");
    await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => {
      const request = new URL(String(input), "https://kyoquan.test");
      return request.pathname === "/api/v1/catalogue"
        && request.searchParams.get("exclude_genre") === "Isekai";
    })).toBe(true));
    expect(new URLSearchParams(window.location.search).get("exclude_genre")).toBe("Isekai");

    await user.click(screen.getByRole("button", { name: "Include", exact: true }));
    await user.click(screen.getByRole("button", { name: "Include Isekai" }));

    await waitFor(() => expect(
      new URLSearchParams(window.location.search).get("genre"),
    ).toBe("Isekai"));
    expect(new URLSearchParams(window.location.search).has("exclude_genre")).toBe(false);
    expect(screen.queryByText("Exclude genre: Isekai")).toBeNull();
  });

  test("filter refreshes retain cards and abort the superseded request", async () => {
    const user = userEvent.setup();
    let summerSignal;
    fetchMock.mockImplementation(async (input, init = {}) => {
      const request = new URL(String(input), "https://kyoquan.test");
      if (request.pathname === "/api/v1/catalogue") {
        const season = request.searchParams.get("season");
        if (season === "summer") {
          summerSignal = init.signal;
          return new Promise(() => {});
        }
        const title = season === "fall" ? "Fall Result" : "Existing Result";
        return {
          ok: true,
          json: async () => ({
            items: [{
              mal_id: season === "fall" ? 2 : 1,
              content_type: "ANIME",
              title,
              genres: [],
            }],
            pagination: { page: 1, pages: 1, total: 1 },
            updated_at: null,
          }),
        };
      }
      return mockCatalogueFetch()(input);
    });
    render(<App />);

    expect(await screen.findByText("Existing Result")).toBeInTheDocument();
    const season = screen.getByRole("combobox", { name: "Season" });
    await user.selectOptions(season, "summer");
    expect(await screen.findByText("Refreshing results...")).toBeInTheDocument();
    expect(screen.getByText("Existing Result")).toBeInTheDocument();

    await user.selectOptions(season, "fall");
    expect(summerSignal.aborted).toBe(true);
    expect(await screen.findByText("Fall Result")).toBeInTheDocument();
  });

  test("a failed refresh removes cards from the previous filter state", async () => {
    const user = userEvent.setup();
    fetchMock.mockImplementation(async (input) => {
      const request = new URL(String(input), "https://kyoquan.test");
      if (request.pathname === "/api/v1/catalogue") {
        if (request.searchParams.get("season") === "summer") {
          return {
            ok: false,
            json: async () => ({ error: { message: "Refresh failed." } }),
          };
        }
        return {
          ok: true,
          json: async () => ({
            items: [{
              mal_id: 1,
              content_type: "ANIME",
              title: "Existing Result",
              genres: [],
            }],
            pagination: { page: 1, pages: 1, total: 1 },
            updated_at: null,
          }),
        };
      }
      return mockCatalogueFetch()(input);
    });
    render(<App />);

    expect(await screen.findByText("Existing Result")).toBeInTheDocument();
    await user.selectOptions(screen.getByRole("combobox", { name: "Season" }), "summer");

    expect(await screen.findByText("Refresh failed.")).toBeInTheDocument();
    expect(screen.queryByText("Existing Result")).toBeNull();
  });

  test("a database outage shows a retryable service message", async () => {
    fetchMock.mockImplementation(async () => ({
      ok: false,
      status: 503,
      json: async () => ({
        error: {
          code: "database_unavailable",
          message: "The catalogue is temporarily unavailable. Please try again shortly.",
        },
      }),
    }));
    const user = userEvent.setup();
    render(<App />);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The catalogue is temporarily unavailable",
    );
    const requestsBeforeRetry = fetchMock.mock.calls.length;
    await user.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThan(
      requestsBeforeRetry,
    ));
  });

  test("slider motion is debounced into one catalogue request", async () => {
    window.history.replaceState({}, "", "/?content_type=MANGA");
    render(<App />);
    await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => (
      String(input).includes("/api/v1/catalogue?")
    ))).toBe(true));
    fetchMock.mockClear();

    const score = screen.getByRole("slider", { name: "Minimum score" });
    fireEvent.change(score, { target: { value: "6" } });
    fireEvent.change(score, { target: { value: "7" } });
    fireEvent.change(score, { target: { value: "8" } });
    expect(screen.getByText("8+")).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([input]) => (
      String(input).includes("min_score=")
    ))).toBe(false);

    await waitFor(() => expect(fetchMock.mock.calls.filter(([input]) => (
      String(input).includes("/api/v1/catalogue?")
      && String(input).includes("min_score=8")
    ))).toHaveLength(1));
  });

  test("Browser navigation cancels a pending slider update", async () => {
    window.history.replaceState({}, "", "/?content_type=MANGA");
    render(<App />);
    await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => (
      String(input).includes("/api/v1/catalogue?")
    ))).toBe(true));
    fetchMock.mockClear();

    fireEvent.change(screen.getByRole("slider", { name: "Minimum score" }), {
      target: { value: "8" },
    });
    window.history.pushState({}, "", "/?content_type=MANGA&min_score=4");
    window.dispatchEvent(new PopStateEvent("popstate"));

    expect(await screen.findByText("4+")).toBeInTheDocument();
    await new Promise((resolve) => window.setTimeout(resolve, 350));
    expect(new URLSearchParams(window.location.search).get("min_score")).toBe("4");
    expect(fetchMock.mock.calls.some(([input]) => (
      String(input).includes("min_score=8")
    ))).toBe(false);
  });

  test("All content exposes only cross-catalogue filters", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/?content_type=ALL");
    render(<App />);

    await user.click(screen.getByRole("button", { name: "More filters" }));
    expect(screen.getByRole("slider", { name: "Minimum score" })).toBeInTheDocument();
    expect(screen.getByRole("slider", { name: "Minimum year" })).toBeInTheDocument();
    expect(screen.queryByRole("slider", { name: "Minimum episodes" })).toBeNull();
    expect(screen.queryByRole("slider", { name: "Minimum chapters" })).toBeNull();
    expect(screen.queryByRole("slider", { name: "Minimum volumes" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Studio" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Streaming Service" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Author" })).toBeNull();
  });

  test("Manga authors support searching, selection, chips, and URL state", async () => {
    const user = userEvent.setup();
    fetchMock.mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes("/api/v1/authors")) {
        return {
          ok: true,
          json: async () => ({ items: ["Hiromu Arakawa", "SIU"] }),
        };
      }
      return mockCatalogueFetch()(input);
    });
    window.history.replaceState({}, "", "/?content_type=MANGA");
    render(<App />);

    await user.click(await screen.findByRole("button", { name: "Author" }));
    const search = screen.getByRole("combobox", { name: "Search author" });
    await user.type(search, "Hiromu");
    await user.click(await screen.findByRole("option", {
      name: "Hiromu Arakawa",
    }));

    expect(await screen.findByText("Author: Hiromu Arakawa")).toBeInTheDocument();
    expect(new URLSearchParams(window.location.search).get("author")).toBe(
      "Hiromu Arakawa",
    );
    expect(fetchMock.mock.calls.some(([input]) => {
      const request = new URL(String(input), "https://kyoquan.test");
      return request.pathname === "/api/v1/catalogue"
        && request.searchParams.get("author") === "Hiromu Arakawa";
    })).toBe(true);
  });

  test("Manga details display author names and roles", async () => {
    const user = userEvent.setup();
    fetchMock.mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes("/api/v1/catalogue/MANGA/1")) {
        return {
          ok: true,
          json: async () => ({
            item: {
              mal_id: 1,
              content_type: "MANGA",
              title: "Example Manga",
              genres: [],
              authors: [
                { name: "Hiromu Arakawa", role: "Story & Art" },
              ],
            },
          }),
        };
      }
      if (url.includes("/api/v1/catalogue?")) {
        return {
          ok: true,
          json: async () => ({
            items: [{
              mal_id: 1,
              content_type: "MANGA",
              title: "Example Manga",
              genres: [],
              authors: [],
            }],
            pagination: { page: 1, pages: 1, total: 1 },
            updated_at: null,
          }),
        };
      }
      return mockCatalogueFetch()(input);
    });
    window.history.replaceState({}, "", "/?content_type=MANGA");
    render(<App />);

    await user.click(await screen.findByText("Example Manga"));

    expect(await screen.findByText("Author:")).toBeInTheDocument();
    expect(screen.getByText("Hiromu Arakawa (Story & Art)")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Close details" }));
    await user.click(screen.getByText("Example Manga"));
    expect(fetchMock.mock.calls.filter(([input]) => (
      String(input).includes("/api/v1/catalogue/MANGA/1")
    ))).toHaveLength(1);
  });

  test("first detail open uses a stable skeleton until the full record arrives", async () => {
    const user = userEvent.setup();
    let resolveDetail;
    fetchMock.mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes("/api/v1/catalogue/ANIME/1")) {
        return new Promise((resolve) => {
          resolveDetail = () => resolve({
            ok: true,
            json: async () => ({
              item: {
                mal_id: 1,
                content_type: "ANIME",
                title: "Example Anime",
                genres: ["Action"],
                popularity: 1,
                members: 100,
                synopsis: "Full details are ready.",
                last_jikan_sync: "2026-09-01T00:00:00Z",
                last_verified_refresh: new Date().toISOString(),
              },
            }),
          });
        });
      }
      if (url.includes("/api/v1/catalogue?")) {
        return {
          ok: true,
          json: async () => ({
            items: [{
              mal_id: 1,
              content_type: "ANIME",
              title: "Example Anime",
              genres: [],
            }],
            pagination: { page: 1, pages: 1, total: 1 },
            updated_at: null,
          }),
        };
      }
      return mockCatalogueFetch()(input);
    });
    render(<App />);

    await user.click(await screen.findByText("Example Anime"));
    expect(await screen.findByText("Loading full details…")).toBeInTheDocument();
    expect(screen.queryByText("Popularity rank")).toBeNull();

    resolveDetail();
    expect(await screen.findByText("Popularity rank")).toBeInTheDocument();
    expect(screen.getByText("Full details are ready.")).toBeInTheDocument();
    expect(screen.getByText(/Details checked with Jikan/)).toBeInTheDocument();
  });

  test("mobile Filters reveals the responsive slider panel", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/?content_type=MANHWA");
    render(<App />);
    const panel = document.getElementById("mobile-more-filters");

    expect(panel).toHaveClass("hidden");
    await user.click(screen.getByRole("button", { name: "Filters" }));
    expect(panel).toHaveClass("grid");
    expect(screen.getByRole("slider", {
      name: "Minimum score",
    })).toBeInTheDocument();
  });
});
