import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { Layout } from "../Layout";

const sessions = [
  {
    session_id: "session-1",
    title: "A very long session title that must truncate",
  },
];

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string) => ({
      "app.version": "v0.1.13",
      "layout.agent": "Agent",
      "layout.alphaZoo": "Alpha Zoo",
      "layout.cancel": "Cancel",
      "layout.collapse": "Collapse",
      "layout.close": "Close",
      "layout.confirm": "Confirm",
      "layout.correlation": "Correlation Matrix",
      "layout.dark": "Dark",
      "layout.delete": "Delete",
      "layout.expand": "Expand",
      "layout.home": "Home",
      "layout.language": "Language",
      "layout.light": "Light",
      "layout.mainNavigation": "Main navigation",
      "layout.menu": "Menu",
      "layout.newChat": "New Chat",
      "layout.noSessions": "No sessions yet",
      "layout.rename": "Rename",
      "layout.reports": "Reports",
      "layout.runtime": "Runtime",
      "layout.sessions": "Sessions",
      "layout.settings": "Settings",
      "layout.sidebar": "Vibe-Trading sidebar",
      "layout.skipToMain": "Skip to main content",
    })[key] ?? key,
    i18n: {
      language: "en",
      languages: ["en"],
      changeLanguage: vi.fn().mockResolvedValue(undefined),
    },
  }),
}));

vi.mock("@/hooks/useDarkMode", () => ({
  useDarkMode: () => ({ dark: false, toggle: vi.fn() }),
}));

vi.mock("@/lib/api", () => ({
  api: {
    listSessions: vi.fn().mockResolvedValue([
      {
        session_id: "session-1",
        title: "A very long session title that must truncate",
      },
    ]),
    deleteSession: vi.fn().mockResolvedValue(undefined),
    renameSession: vi.fn().mockResolvedValue(undefined),
  },
}));

vi.mock("@/stores/agent", () => ({
  useAgentStore: (selector: (state: {
    sseStatus: string;
    sseRetryAttempt: number;
    streamingSessionId: null;
  }) => unknown) => selector({
    sseStatus: "connected",
    sseRetryAttempt: 0,
    streamingSessionId: null,
  }),
}));

function renderLayout() {
  return render(
    <MemoryRouter initialEntries={["/agent"]}>
      <Routes>
        <Route element={<Layout />}>
          <Route path="/agent" element={<div>Agent content</div>} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

describe("Layout accessibility", () => {
  it("labels landmarks, brand, main content, and the new-chat affordance", () => {
    renderLayout();

    const sidebar = screen.getByRole("complementary", { name: "Vibe-Trading sidebar" });
    expect(sidebar).toHaveClass("max-md:hidden");
    expect(screen.getByRole("navigation", { name: "Main navigation" })).toBeInTheDocument();
    // Scoped to the desktop <aside> — the mobile header bar has its own,
    // separate "Vibe-Trading" brand link (jsdom renders both; only one is
    // ever visible in a real browser, decided by the `max-md:hidden` CSS
    // breakpoint neither jsdom nor these role queries evaluate).
    expect(within(sidebar).getByRole("link", { name: "Vibe-Trading" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "New Chat" })).toHaveAttribute("title", "New Chat");
    expect(screen.getByText("Skip to main content")).toHaveAttribute("href", "#main");
    expect(screen.getByRole("main")).toHaveAttribute("id", "main");
    expect(screen.getByRole("main").parentElement).toHaveClass("relative");
  });

  it("exposes session actions on keyboard focus and labels the rename input", async () => {
    renderLayout();

    const title = await screen.findByText(sessions[0].title);
    expect(title).toHaveClass("min-w-0", "truncate");

    const renameButton = screen.getByRole("button", { name: "Rename" });
    expect(renameButton.parentElement).toHaveClass("group-focus-within:opacity-100");
    fireEvent.click(renameButton);

    expect(screen.getByRole("textbox", { name: `Rename: ${sessions[0].title}` })).toHaveClass(
      "focus:ring-2",
      "focus:ring-primary/40",
    );
  });

  it("does not crash when localStorage access is blocked", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("Blocked", "SecurityError");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("Blocked", "SecurityError");
    });

    expect(() => renderLayout()).not.toThrow();
  });

  it("uses button disclosure semantics for the language switcher", () => {
    renderLayout();

    const languageButton = screen.getByRole("button", { name: "Language" });
    expect(languageButton).toHaveAttribute("aria-expanded", "false");
    expect(languageButton).not.toHaveAttribute("aria-haspopup");
  });

  it("opens the full sidebar as a dismissible drawer from the mobile-only hamburger trigger", async () => {
    renderLayout();
    await screen.findByText(sessions[0].title);

    expect(screen.queryByRole("dialog", { name: "Vibe-Trading sidebar" })).not.toBeInTheDocument();

    // The sidebar <aside> is `max-md:hidden` entirely on mobile — this
    // hamburger button in the mobile header bar is the only way in.
    fireEvent.click(screen.getByRole("button", { name: "Menu" }));

    const dialog = screen.getByRole("dialog", { name: "Vibe-Trading sidebar" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    // The drawer is a full, self-contained copy of the sidebar's content —
    // nav links and the session list both — not just the sessions panel.
    expect(within(dialog).getByRole("link", { name: "Agent" })).toBeInTheDocument();
    expect(within(dialog).getByRole("link", { name: "Runtime" })).toBeInTheDocument();
    expect(within(dialog).getByText(sessions[0].title)).toBeInTheDocument();
    // Slides/fades in rather than appearing instantly — mounts off-screen
    // (translate-x-full) and transparent, then the open handler's rAF flips
    // it to the visible transform/opacity a frame later.
    expect(dialog.className).toContain("transition-transform");
    await waitFor(() => expect(dialog.className).toContain("translate-x-0"));

    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    // The drawer plays a slide/fade-out transition before unmounting —
    // see MOBILE_SIDEBAR_TRANSITION_MS — so this is async, not immediate.
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "Vibe-Trading sidebar" })).not.toBeInTheDocument();
    });
  });

  it("closes the mobile drawer via Escape and via clicking the backdrop", async () => {
    renderLayout();
    await screen.findByText(sessions[0].title);
    const trigger = screen.getByRole("button", { name: "Menu" });

    fireEvent.click(trigger);
    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "Vibe-Trading sidebar" })).not.toBeInTheDocument();
    });

    fireEvent.click(trigger);
    const dialog = screen.getByRole("dialog", { name: "Vibe-Trading sidebar" });
    // The backdrop is the dialog's own positioning parent, one level up.
    fireEvent.click(dialog.parentElement!);
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "Vibe-Trading sidebar" })).not.toBeInTheDocument();
    });
  });

  it("closes the mobile drawer after navigating to a session, or a nav link, from it", async () => {
    renderLayout();
    await screen.findByText(sessions[0].title);

    fireEvent.click(screen.getByRole("button", { name: "Menu" }));
    let dialog = screen.getByRole("dialog", { name: "Vibe-Trading sidebar" });
    fireEvent.click(within(dialog).getByText(sessions[0].title));
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "Vibe-Trading sidebar" })).not.toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: "Menu" }));
    dialog = screen.getByRole("dialog", { name: "Vibe-Trading sidebar" });
    fireEvent.click(within(dialog).getByRole("link", { name: "Runtime" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "Vibe-Trading sidebar" })).not.toBeInTheDocument();
    });
  });

  it("synchronizes the sidebar preference from another tab", () => {
    window.localStorage.setItem("qa-sidebar", "expanded");
    renderLayout();
    const sidebar = screen.getByRole("complementary", { name: "Vibe-Trading sidebar" });
    expect(sidebar).toHaveClass("w-64");

    window.localStorage.setItem("qa-sidebar", "collapsed");
    fireEvent(window, new StorageEvent("storage", { key: "qa-sidebar" }));

    expect(sidebar).toHaveClass("w-12");
  });
});
