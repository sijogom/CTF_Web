import { TerminalHero } from "@/components/landing/TerminalHero";
import { LiveStats } from "@/components/landing/LiveStats";
import { GrowthTimeline } from "@/components/landing/GrowthTimeline";
import { FeatureTabs } from "@/components/landing/FeatureTabs";
import { ActivityFeed } from "@/components/landing/ActivityFeed";
import { FinalCTA } from "@/components/landing/FinalCTA";

export default function HomePage() {
  return (
    <div className="-mx-6 -mt-8">
      <TerminalHero />
      <LiveStats />
      <GrowthTimeline />
      <FeatureTabs />
      <ActivityFeed />
      <FinalCTA />
    </div>
  );
}
