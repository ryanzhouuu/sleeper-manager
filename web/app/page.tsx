import {
  GamesBlock,
  RecommendationBlock,
  RosterBlock,
  UnavailableBlock,
  WeekBlock,
} from "@/components/blocks";
import { loadHome } from "@/lib/home";
import { managerControlsUrl } from "@/lib/controls";

// Without this, a build with HOME_URL unset would prerender the unavailable page forever.
export const dynamic = "force-dynamic";

export default async function HomePage() {
  const result = await loadHome(process.env.HOME_URL);
  if (result.status !== "ok") {
    return (
      <main className="page">
        <UnavailableBlock />
      </main>
    );
  }
  const { league, matchup, roster, games, recommendation } = result.document;
  const controlsUrl = managerControlsUrl(process.env.HOME_URL);
  return (
    <main className="page split">
      <div className="column">
        <WeekBlock league={league} matchup={matchup} />
        <RecommendationBlock recommendation={recommendation} />
        {controlsUrl ? (
          <section className="block">
            <a className="controls-link" href={controlsUrl}>
              update lock / pass decisions →
            </a>
          </section>
        ) : null}
        <RosterBlock roster={roster} />
      </div>
      <div className="column">
        <GamesBlock games={games} timeZone={league.timezone} />
      </div>
    </main>
  );
}
