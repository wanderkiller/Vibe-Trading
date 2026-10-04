import { BrandMark } from "@/components/common/BrandMark";

// Hidden below `md`: at 32px plus its parent's `gap-3` (12px), it pushes
// assistant message text 44px further right than user messages or the
// composer — barely noticeable on desktop, but on a phone-width screen
// that's a visible misalignment eating into already-scarce line width.
// `display: none` drops it from the flex layout entirely, so the parent's
// `gap` collapses along with it — text ends up flush with the same
// padding edge everything else in the shell already uses.
export function AgentAvatar() {
  return (
    <div className="hidden md:block h-8 w-8 shrink-0 mt-0.5" aria-hidden="true">
      <BrandMark className="h-8 w-8" />
    </div>
  );
}
