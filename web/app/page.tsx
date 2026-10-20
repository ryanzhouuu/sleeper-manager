import {
  GamesBlock,
  RecommendationBlock,
  RosterBlock,
  UnavailableBlock,
  WeekBlock,
} from "@/components/blocks";
import { loadHome } from "@/lib/home";

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
  return (
    <main className="page">
      <WeekBlock league={league} matchup={matchup} />
      <RecommendationBlock recommendation={recommendation} />
      <RosterBlock roster={roster} />
      <GamesBlock games={games} timeZone={league.timezone} />
    </main>
  );
}
