"use client";

/** Every panel is server-rendered, so read-only history stays available before hydration. */
import { useRef, useState, type KeyboardEvent, type ReactNode } from "react";

export interface InfoTab {
  id: string;
  label: string;
  content: ReactNode;
}

const STEPS: Record<string, number> = { ArrowRight: 1, ArrowLeft: -1 };

export function InfoTabs({ label, tabs }: { label: string; tabs: InfoTab[] }) {
  const [selected, setSelected] = useState(tabs[0]?.id);
  const buttons = useRef<(HTMLButtonElement | null)[]>([]);

  function moveFocus(event: KeyboardEvent, index: number) {
    const step = STEPS[event.key];
    if (!step) return;
    event.preventDefault();
    const next = (index + step + tabs.length) % tabs.length;
    setSelected(tabs[next].id);
    buttons.current[next]?.focus();
  }

  return (
    <section className="info-tabs">
      <div className="info-tablist" role="tablist" aria-label={label}>
        {tabs.map((tab, index) => (
          <button
            key={tab.id}
            ref={(element) => {
              buttons.current[index] = element;
            }}
            id={`info-tab-${tab.id}`}
            className="info-tab"
            type="button"
            role="tab"
            aria-selected={tab.id === selected}
            aria-controls={`info-panel-${tab.id}`}
            tabIndex={tab.id === selected ? 0 : -1}
            onClick={() => setSelected(tab.id)}
            onKeyDown={(event) => moveFocus(event, index)}
          >
            {tab.label}
          </button>
        ))}
      </div>
      {tabs.map((tab) => (
        <div
          key={tab.id}
          id={`info-panel-${tab.id}`}
          className="info-panel"
          role="tabpanel"
          aria-labelledby={`info-tab-${tab.id}`}
          tabIndex={0}
          hidden={tab.id !== selected}
        >
          {tab.content}
        </div>
      ))}
    </section>
  );
}
