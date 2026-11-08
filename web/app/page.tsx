import {
  GamesBlock,
  RecommendationBlock,
  UnavailableBlock,
  WeekBlock,
} from "@/components/blocks";
import { InfoTabs } from "@/components/info-tabs";
import { LineupBlock, PlanBlock } from "@/components/lineup-block";
import { loadHome } from "@/lib/home";

// Without this, a build with HOME_URL unset would prerender the unavailable page forever.
export const dynamic = "force-dynamic";

function HelpPanel() {
  return (
    <div className="help">
      <p>
        Lock state comes from Sleeper. Lock or swap players in the Sleeper app;
        this page reads the result.
      </p>
      <p>
        lockable: a finished game you can still lock. passed: the lock window
        closed or eligibility was lost. unverified: the app could not confirm
        the player was starting at tipoff — answer below the game.
      </p>
      <p>
        Decisions are the planner’s advice: lock now, keep starting to stay
        lockable, or bench for a substitute and give up that game.
      </p>
    </div>
  );
}

export default async function HomePage() {
  const result = await loadHome(process.env.HOME_URL);
  if (result.status !== "ok") {
    return (
      <main className="page">
        <UnavailableBlock />
      </main>
    );
  }
  const { league, matchup, games, recommendation, lineup, plan, lock_data } =
    result.document;
  return (
    <main className="page split">
      <div className="column">
        <WeekBlock league={league} matchup={matchup} />
        {lock_data === "unavailable" ? (
          <section className="block" role="alert">
            <p className="heading urgent">
              Lock data unavailable from Sleeper. Lock states and advice may be
              out of date.
            </p>
          </section>
        ) : null}
        <PlanBlock plan={plan} />
        <RecommendationBlock recommendation={recommendation} />
        <LineupBlock lineup={lineup} timeZone={league.timezone} />
      </div>
      <div className="column">
        <InfoTabs
          label="Week details"
          tabs={[
            {
              id: "week",
              label: "this week",
              content: <GamesBlock games={games} timeZone={league.timezone} />,
            },
            { id: "help", label: "help", content: <HelpPanel /> },
          ]}
        />
      </div>
    </main>
  );
}
