# GENERATED from contract/openapi.json by gen_models.sh (datamodel-code-generator). Do not edit.

from __future__ import annotations

from enum import Enum, StrEnum
from typing import Annotated, Any
from pydantic import BaseModel, ConfigDict, Field, RootModel
from typing_extensions import TypeAliasType


class AdjustmentField(StrEnum):
    qty = "qty"
    limit_price = "limit_price"


class AdjustmentReason(StrEnum):
    precision_toward_zero = "precision_toward_zero"
    f64_roundtrip = "f64_roundtrip"
    price_precision = "price_precision"
    price_rounding_conservative = "price_rounding_conservative"


class AlwaysFalse(Enum):
    boolean_False = False


BTreeMapAdditionalProperty = TypeAliasType(
    "BTreeMapAdditionalProperty", Annotated[int, Field(ge=0)]
)


class BTreeMap(RootModel[dict[str, BTreeMapAdditionalProperty]]):
    root: dict[str, BTreeMapAdditionalProperty]


class Ccy(StrEnum):
    USDT = "USDT"
    USDC = "USDC"


class ComparisonSchema(StrEnum):
    alphakeel_comparison_1 = "alphakeel.comparison/1"


class DataLockCoverageFunding(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    complete: Annotated[int, Field(ge=0)]
    none: Annotated[int, Field(ge=0)]
    partial: Annotated[int, Field(ge=0)]
    perps: Annotated[int, Field(ge=0)]


class DataLockCoverageFx(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    frames_without_rate: Annotated[int, Field(ge=0)]
    points: Annotated[int, Field(ge=0)]


class DataLockCoverageQuotes(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    frames: Annotated[int, Field(ge=0)]
    frames_all_venues_failed: Annotated[int, Field(ge=0)]
    rows: Annotated[int, Field(ge=0)]


class DataLockFundingVisibility(StrEnum):
    historical_reconstruction = "historical_reconstruction"
    observed_pit = "observed_pit"


class DataLockRequestInclude(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    native: bool
    tables: bool


class DataLockSchema(StrEnum):
    alphakeel_data_lock_1 = "alphakeel.data-lock/1"


class DataLockSelectionKind(StrEnum):
    declared = "declared"
    pit_membership = "pit_membership"
    screener_live = "screener_live"


class DataLockSourcesItemRole(StrEnum):
    funding_settlement = "funding_settlement"
    funding_observation = "funding_observation"
    quote = "quote"
    fx = "fx"
    fee = "fee"
    instrument = "instrument"
    config = "config"
    market = "market"


class DataLockSourcesItemVisibility(StrEnum):
    observed_pit = "observed_pit"
    historical_reconstruction = "historical_reconstruction"
    static = "static"


class DataLockUniverseByVenueValue(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    perp: Annotated[int, Field(ge=0)]
    spot: Annotated[int, Field(ge=0)]


class DataObjectCodec(StrEnum):
    jsonl_zstd = "jsonl+zstd"
    jsonl = "jsonl"
    json = "json"


class DataObjectRole(StrEnum):
    quotes = "quotes"
    observations = "observations"
    settlements = "settlements"
    frames = "frames"
    fx = "fx"
    instruments = "instruments"
    fees = "fees"
    scans = "scans"
    config = "config"
    coverage = "coverage"
    market = "market"


class DatasetSpec(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    market_dataset: Annotated[
        str | None,
        Field(
            description="Market dataset version; empty = latest when a market dataset is configured and `market_tables` is non-empty."
        ),
    ] = None
    market_tables: Annotated[
        list[str] | None,
        Field(
            description='Subset of `kline_1m`, `bbo`, `depth20`, `instrument_meta`, `kline_1h`, `kline_1d`, `borrow_lend` (`borrow_lend` also needs `{venue, market: "lend", symbol: <quote currency>}` entries in `instruments`).'
        ),
    ] = None
    price_kinds: Annotated[
        list[str] | None,
        Field(
            description='For `kline_1m`: subset of `trade`, `mark`, `index` (default `["trade"]`).'
        ),
    ] = None


class Dec(RootModel[str]):
    root: Annotated[
        str,
        Field(
            description="精确十进制：可带负号，无指数、无空白。",
            pattern="^-?[0-9]{1,40}(\\.[0-9]{1,40})?$",
        ),
    ]


class DifferenceClass(StrEnum):
    input = "input"
    transform = "transform"
    profile = "profile"
    decision = "decision"
    fill = "fill"
    fee = "fee"
    funding = "funding"
    fx = "fx"
    rounding = "rounding"
    aggregate = "aggregate"
    ledger = "ledger"
    evidence = "evidence"


class DifferenceLayer(StrEnum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"
    L4 = "L4"


class EventDataBalance(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    account: Annotated[str, Field(max_length=60, min_length=1)]
    ccy: Ccy
    total: Dec


class EventDataFillLiquidity(StrEnum):
    taker = "taker"


class EventDataFundingRateSource(StrEnum):
    official_settlement = "official_settlement"
    predicted_estimate = "predicted_estimate"


class EventDataIntentSide(StrEnum):
    buy = "buy"
    sell = "sell"


class EventDataOrderResultStatus(StrEnum):
    filled = "filled"
    partially_filled = "partially_filled"
    rejected = "rejected"
    denied = "denied"
    cancelled = "cancelled"
    not_submitted = "not_submitted"


class EventDataPositionChange(StrEnum):
    opened = "opened"
    increased = "increased"
    reduced = "reduced"
    closed = "closed"


class EventDataPositionSide(StrEnum):
    long = "long"
    short = "short"
    flat = "flat"


class EventKind(StrEnum):
    decision = "decision"
    intent = "intent"
    order_result = "order_result"
    fill = "fill"
    fee = "fee"
    funding = "funding"
    position = "position"
    balance = "balance"
    equity = "equity"


class EventsHeaderSchema(StrEnum):
    alphakeel_events_1 = "alphakeel.events/1"


class ExecutionProfileAccountLeverageSpot(StrEnum):
    field_1 = "1"


class ExecutionProfileAccountLiquidation(StrEnum):
    disabled = "disabled"
    conservative_tiered_margin = "conservative_tiered_margin"


class ExecutionProfileAccountMarginMode(StrEnum):
    per_venue_margin = "per_venue_margin"


class ExecutionProfileAccountPositionMode(StrEnum):
    hedging = "hedging"


class ExecutionProfileAccountSameInstantMargin(StrEnum):
    per_order_engine = "per_order_engine"
    cumulative_initial_margin = "cumulative_initial_margin"


class ExecutionProfileDecisionClockKind(StrEnum):
    scan = "scan"


class ExecutionProfileEndOfRunOpenPositions(StrEnum):
    valued_close_now_not_closed = "valued_close_now_not_closed"


class ExecutionProfileEventOrder(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    same_time: Annotated[list[str], Field(max_length=12, min_length=1)]
    tie_ns: bool


class ExecutionProfileFeesMaker(StrEnum):
    same_as_taker = "same_as_taker"
    per_venue_market_maker = "per_venue_market_maker"


class ExecutionProfileFeesModel(StrEnum):
    taker_rate_per_venue_market = "taker_rate_per_venue_market"


class ExecutionProfileFeesRounding(StrEnum):
    currency_min_unit_1e_8_per_fill = "currency_min_unit_1e-8_per_fill"


class ExecutionProfileFundingMode(StrEnum):
    official_settlement = "official_settlement"
    estimate = "estimate"


class ExecutionProfileFundingNotional(StrEnum):
    abs_qty_times_settlement_price = "abs_qty_times_settlement_price"


class ExecutionProfileFundingRounding(StrEnum):
    currency_min_unit_1e_8 = "currency_min_unit_1e-8"


class ExecutionProfileFundingSettlementPrice(StrEnum):
    last_mark_price = "last_mark_price"
    official_mark_when_published_else_last_frame_mark = (
        "official_mark_when_published_else_last_frame_mark"
    )


class ExecutionProfileFundingSign(StrEnum):
    long_pays_when_rate_positive = "long_pays_when_rate_positive"


class ExecutionProfileFxEndValue(StrEnum):
    last_known_rate = "last_known_rate"


class ExecutionProfileFxFillTime(StrEnum):
    rate_at_fill_scan = "rate_at_fill_scan"


class ExecutionProfileFxPair(StrEnum):
    USDC_USDT = "USDC/USDT"


class ExecutionProfileFxRealized(StrEnum):
    rate_at_close_scan = "rate_at_close_scan"


class ExecutionProfileFxSource(StrEnum):
    scan_mid_of_usdc_usdt_spot = "scan_mid_of_usdc_usdt_spot"


class ExecutionProfileInitialStateCash(StrEnum):
    starting_balance_per_venue_and_currency = "starting_balance_per_venue_and_currency"


class ExecutionProfileInitialStatePositions(StrEnum):
    flat = "flat"


class ExecutionProfileMatchingDepth(StrEnum):
    not_modeled = "not_modeled"


class ExecutionProfileMatchingModel(StrEnum):
    top_of_book_ioc = "top_of_book_ioc"
    top_of_book_ioc_with_post_only_entry = "top_of_book_ioc_with_post_only_entry"


class ExecutionProfileMatchingOrderTypesItem(StrEnum):
    limit_ioc = "limit_ioc"
    market = "market"


class ExecutionProfilePrecisionMoneyUnit(StrEnum):
    field_1e_8 = "1e-8"


class ExecutionProfilePrecisionPrice(StrEnum):
    inferred_from_data_max16 = "inferred_from_data_max16"


class ExecutionProfilePrecisionPriceRounding(StrEnum):
    buy_up_sell_down = "buy_up_sell_down"


class ExecutionProfilePrecisionQuantityRounding(StrEnum):
    toward_zero_f64_stable = "toward_zero_f64_stable"


class ExecutionProfilePrecisionSize(StrEnum):
    inferred_from_intents_max16 = "inferred_from_intents_max16"
    fixed_8_places = "fixed_8_places"


class ExecutionProfileQuotesDecisionVisibility(StrEnum):
    quote_ts_not_after_decision = "quote_ts_not_after_decision"


class ExecutionProfileSamplingDrawdown(StrEnum):
    close_now_from_zero_peak = "close_now_from_zero_peak"


class ExecutionProfileSamplingValuation(StrEnum):
    each_scan = "each_scan"


class ExecutionProfileSchema(StrEnum):
    alphakeel_execution_profile_1 = "alphakeel.execution-profile/1"


class ExecutionProfileWarmup(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    frames: Annotated[int, Field(ge=0)]
    trades_allowed: AlwaysFalse


class FeeOverride(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    perp_taker: str
    spot_taker: str | None = None


class HashOrEmpty(RootModel[str]):
    root: Annotated[
        str,
        Field(
            description="空串或 SHA-256（链锚点：首份没有上一份）。",
            pattern="^([0-9a-f]{64})?$",
        ),
    ]


class Id(RootModel[str]):
    root: Annotated[
        str,
        Field(
            description="稳定标识：字母数字开头，可含 `._:/-`，最长 128。",
            pattern="^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$",
        ),
    ]


class InputAccessTable(StrEnum):
    quotes = "quotes"
    observations = "observations"
    settlements = "settlements"
    fx = "fx"
    instruments = "instruments"
    frames = "frames"
    kline_1m = "kline_1m"
    kline_1h = "kline_1h"
    kline_1d = "kline_1d"
    instrument_meta = "instrument_meta"
    borrow_lend = "borrow_lend"


class InputFrames(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    count: Annotated[int, Field(ge=0)]
    first_seq: Annotated[int, Field(ge=0)]
    first_sha256: Annotated[
        str | None,
        Field(
            description="SHA-256 of the first / last replayed scan frame (the archive's frame hash, the same value a paper journal Mark carries in\n`input.sha256`). AlphaKeel's G3 item 3.9 compares them with the paper's frames at the segment boundaries (frame identity)."
        ),
    ] = None
    last_seq: Annotated[int, Field(ge=0)]
    last_sha256: str | None = None


class IntentsSchema(StrEnum):
    alphakeel_intents_1 = "alphakeel.intents/1"


class Market(StrEnum):
    perp = "perp"
    spot = "spot"


class Mode(StrEnum):
    native_strategy = "native_strategy"
    fixed_intent_replay = "fixed_intent_replay"
    python_policy = "python_policy"
    ir_strategy = "ir_strategy"


class Ns(RootModel[str]):
    root: Annotated[
        str,
        Field(
            description="纳秒（无损）：数字字符串，不得有前导零。",
            pattern="^(0|[1-9][0-9]{0,19})$",
        ),
    ]


class PDec(RootModel[str]):
    root: Annotated[
        str,
        Field(
            description="严格为正的精确十进制。",
            pattern="^([0-9]{0,39}[1-9][0-9]{0,39}(\\.[0-9]{1,40})?|[0-9]{1,40}\\.[0-9]{0,39}[1-9][0-9]{0,39})$",
        ),
    ]


class PackInstrumentRef(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    market: str
    symbol: str
    venue: str


class PackSelection1(StrEnum):
    declared = "declared"


class PitMembership(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    table_sha256: Annotated[
        str | None,
        Field(
            description="Optional: the SHA-256 of the published membership table the pack must be built from (the `membership.table_sha256`\nof an earlier pack's data lock). A later receipt of the same dataset version may carry an appended or upgraded\ntable; with this set the build is rejected on a mismatch, so a pinned pack is reproducible."
        ),
    ] = None
    top_n: Annotated[
        int,
        Field(
            description="Daily membership depth N (the first N members of each day are used; the table must have been built with N or more).",
            ge=0,
        ),
    ]


class PolicyContextAccountsItem(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    account: Annotated[str, Field(max_length=60, min_length=1)]
    ccy: Ccy
    free: Dec
    total: Dec


class PolicyContextDataRefs(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    decided_ms: Annotated[int, Field(ge=0)]
    frame_index: Annotated[int, Field(ge=0)]
    frame_seq: Annotated[int, Field(ge=0)]


class PolicyContextPositionsItemSide(StrEnum):
    long = "long"
    short = "short"


class PolicyContextSchema(StrEnum):
    alphakeel_policy_context_1 = "alphakeel.policy-context/1"


class PolicyResponseSchema(StrEnum):
    alphakeel_policy_response_1 = "alphakeel.policy-response/1"


class ResultAmounts(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    close_now: Dec | None
    fees: Dec
    funding: Dec
    fx_revaluation: Dec | None
    liquidation_penalty: Dec | None = None
    net_of_capital_cost: Dec | None = None
    open_value: Dec | None
    price_pnl: Dec
    realized: Dec


class ResultByCurrencyValue(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    cash_flow: Dec
    fees: Dec
    funding: Dec


class ResultEquity(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    end_by_ccy: dict[str, Dec]
    end_close_now_usdt: Dec | None
    start_by_ccy: dict[str, Dec]


class ResultFillModel(StrEnum):
    conservative = "conservative"
    top_of_book = "top_of_book"


class ResultSchema(StrEnum):
    alphakeel_result_1 = "alphakeel.result/1"


class ResultWindow(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    end_ms: Annotated[int, Field(ge=0)]
    start_ms: Annotated[int, Field(ge=0)]


class ReviewVerdictCounts(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    fail: Annotated[int, Field(ge=0)]
    not_judged: Annotated[int, Field(ge=0)]
    pass_: Annotated[int, Field(alias="pass", ge=0)]


class ReviewVerdictSummary(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    counts: ReviewVerdictCounts
    passed: bool | None


class RunManifestBuilder(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    adapter: Annotated[str, Field(max_length=200, min_length=1)]
    build: Annotated[str, Field(max_length=200, min_length=1)]
    dependency_lock_sha256: HashOrEmpty
    dirty: str | None


class RunManifestEngine(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    name: Annotated[str, Field(max_length=200, min_length=1)]
    version: Annotated[str, Field(max_length=200, min_length=1)]


class RunManifestOrigin(StrEnum):
    service = "service"
    client_uploaded = "client_uploaded"
    legacy = "legacy"


class RunManifestSchema(StrEnum):
    alphakeel_run_manifest_1 = "alphakeel.run-manifest/1"


class RunManifestSide(StrEnum):
    alphakeel_engine = "alphakeel_engine"
    python_local = "python_local"
    python_external = "python_external"


class RunManifestStatus(StrEnum):
    queued = "queued"
    running = "running"
    waiting_policy = "waiting_policy"
    complete = "complete"
    failed = "failed"
    cancelled = "cancelled"
    interrupted = "interrupted"


class Sha256(RootModel[str]):
    root: Annotated[
        str, Field(description="小写十六进制 SHA-256。", pattern="^[0-9a-f]{64}$")
    ]


class Status(StrEnum):
    matched = "matched"
    mismatch = "mismatch"
    not_comparable = "not_comparable"
    insufficient_evidence = "insufficient_evidence"
    not_applicable = "not_applicable"


class StrategyManifestContract(StrEnum):
    standalone = "standalone"
    policy = "policy"


class StrategyManifestEntry(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    initialize: Annotated[str | None, Field(max_length=120)] = None
    on_step: Annotated[str | None, Field(max_length=120)] = None
    script: Annotated[str, Field(max_length=240, min_length=1)]


class StrategyManifestFilesItem(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    path: Annotated[str, Field(max_length=240, min_length=1)]
    sha256: Sha256
    size: Annotated[int, Field(ge=0)]


class StrategyManifestRequires(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    fields: Annotated[list[str], Field(max_length=40)]
    history_len: Annotated[int, Field(ge=0)]


class StrategyManifestSchema(StrEnum):
    alphakeel_strategy_manifest_1 = "alphakeel.strategy-manifest/1"


class StrategyManifestSupports(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    modes: Annotated[list[Mode], Field(max_length=3, min_length=1)]
    profile_ids: Annotated[list[Id], Field(max_length=8, min_length=1)]


class Text(RootModel[str]):
    root: Annotated[
        str, Field(description="说明文字（≤2000 字符）。", pattern="^[\\s\\S]{0,2000}$")
    ]


class UDec(RootModel[str]):
    root: Annotated[
        str,
        Field(description="非负精确十进制。", pattern="^[0-9]{1,40}(\\.[0-9]{1,40})?$"),
    ]


class ValidateIrProblem(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    code: Annotated[str, Field(max_length=80, min_length=3)]
    message: Text


class ValidateIrRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    ir: Annotated[
        dict[str, Any],
        Field(
            description="The full `alphakeel.strategy-ir/1` document (shape: `docs/contracts/strategy-ir/schema.json`). Kept as raw JSON\nhere so that a malformed document is reported as a problem with its `ir.*` code instead of a 400."
        ),
    ]
    pack_id: Id | None = None


class ValidateIrResponse(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    problems: Annotated[list[ValidateIrProblem], Field(max_length=20)]
    strategy_id: Id | None
    strategy_sha256: Sha256 | None
    valid: bool


class Venue(StrEnum):
    binance = "binance"
    okx = "okx"
    bybit = "bybit"
    bitget = "bitget"
    gate = "gate"
    aster = "aster"
    hyperliquid = "hyperliquid"
    lighter = "lighter"
    backpack = "backpack"


class VerificationEndEquity(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    equity: Annotated[
        Dec,
        Field(
            description="The decision ledger's equity at that mark (USDT, decimal string, not rounded)."
        ),
    ]
    seq: Annotated[
        int,
        Field(
            description="Archive sequence number of the input frame of the replay's last mark.",
            ge=0,
        ),
    ]


class VerificationEventKind(StrEnum):
    open = "open"
    close = "close"
    funding = "funding"


class Window(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    end_ms: Annotated[int, Field(ge=0)]
    start_ms: Annotated[int, Field(ge=0)]


class WindowMs(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    end_ms: int
    start_ms: int


class Adjustment(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    field: AdjustmentField
    from_: Annotated[Dec, Field(alias="from")]
    reason: AdjustmentReason
    to: Dec


class Check(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    id: Id
    layer: DifferenceLayer
    status: Status


class DailyEquityPoint(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    day_ms: Annotated[int, Field(description="UTC day start, milliseconds.")]
    net: Annotated[
        Dec,
        Field(
            description="The run's net at the day's last valued point (decimal string)."
        ),
    ]


class DataLockCoverage(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    funding: DataLockCoverageFunding
    fx: DataLockCoverageFx
    pnl_backtest_complete: bool
    quotes: DataLockCoverageQuotes
    reasons: Annotated[list[Text], Field(max_length=100)]


class DataLockDatasetsItem(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    dataset_version: Annotated[str, Field(max_length=200, min_length=1)]
    name: Annotated[
        str, Field(description="`funding` or `market`.", max_length=40, min_length=1)
    ]
    schema_version: Annotated[str, Field(max_length=40, min_length=1)]
    snapshot_sha256: Sha256
    tables: Annotated[list[str], Field(max_length=16)]
    venues: Annotated[list[Venue], Field(max_length=9)]
    visibility: DataLockFundingVisibility
    watermark_ms: Annotated[int | None, Field(ge=0)]


class DataLockFeesOverride(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    frames_applied: Annotated[int, Field(description="Scan frames that used it.", ge=0)]
    venues: dict[str, FeeOverride]


class DataLockFunding(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    assumed_delay_ms: Annotated[int, Field(ge=0)]
    boundary_match: Annotated[str, Field(max_length=80, min_length=1)]
    conflict_policy: Annotated[str, Field(max_length=120, min_length=1)]
    dataset_version: Annotated[str, Field(max_length=200, min_length=1)]
    snapshot_sha256: Sha256
    visibility: DataLockFundingVisibility
    watermark_ms: Annotated[int | None, Field(ge=0)]


class DataLockMembership(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    days_in_window: Annotated[int, Field(ge=0)]
    first_day: str
    last_day: str
    table_sha256: Sha256


class DataLockScans(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    anchor_sha256: HashOrEmpty
    committed_last_seq: Annotated[int, Field(ge=0)]
    end_seq: Annotated[int, Field(ge=0)]
    first_ms: Annotated[int, Field(ge=0)]
    frames: Annotated[int, Field(ge=0)]
    gaps_over_3_minutes: Annotated[int, Field(ge=0)]
    instrument_gaps: Annotated[
        BTreeMap | None,
        Field(
            description="Universe packs (no requested instruments): per perpetual (`venue|perp|symbol`), scans between its first and last\nappearance in the window where its venue had data but it was absent from the frame (non-zero entries only). Any\nentry makes `coverage.pnl_backtest_complete` false. Absent in packs frozen before this field existed."
        ),
    ] = None
    instrument_missing: Annotated[
        BTreeMap | None,
        Field(
            description="Per requested instrument (`venue|market|symbol`): scans in the window where the instrument's venue had data but the\ninstrument was absent from the frame. Absent in packs frozen before this field existed."
        ),
    ] = None
    last_ms: Annotated[int, Field(ge=0)]
    manifest_sha256: Sha256
    max_gap_ms: Annotated[int, Field(ge=0)]
    scope_missing: Annotated[
        BTreeMap | None,
        Field(
            description="Per requested venue: scans in the window where the venue was absent from the frame."
        ),
    ] = None
    start_seq: Annotated[int, Field(ge=0)]
    venue_failures: BTreeMap


class DataLockSelection(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    kind: DataLockSelectionKind
    membership: DataLockMembership | None = None
    top_n: Annotated[int | None, Field(ge=0)] = None


class DataLockSourcesItem(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    fee_source: Annotated[
        str | None,
        Field(
            description='Fee role only: "recorded" (scan configs) or "request_override" (request fees_override applied).'
        ),
    ] = None
    identity: Annotated[str, Field(max_length=400, min_length=1)]
    note: Text | None = None
    role: DataLockSourcesItemRole
    visibility: DataLockSourcesItemVisibility
    watermark_ms: Annotated[int | None, Field(ge=0)]


class DataLockTransformsItem(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    code_version: Annotated[str, Field(max_length=120, min_length=1)]
    config_sha256: Sha256
    id: Id
    inputs: Annotated[list[Sha256], Field(max_length=100000)]
    outputs: Annotated[list[Sha256], Field(max_length=100000)]


class DataLockUnits(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    money: Text
    price: Text
    quantity: Text
    rate: Text
    time: Text


class DataLockUniverse(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    by_venue: dict[str, DataLockUniverseByVenueValue]
    digest: Sha256
    instruments: Annotated[int, Field(ge=0)]
    selected: Annotated[int, Field(ge=0)]
    selection: DataLockSelection | None = None
    survivorship: Annotated[
        dict[str, str] | None,
        Field(
            description="Per venue, the worse of the funding-history side and the market side:\n`includes_delisted` < `includes_delisted_from_archive` < `survivors_before_first_seen` < `survivors_only_unobserved`."
        ),
    ] = None


class DataLockWindows(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    actual: Window
    requested: Window
    warmup: Window | None


class DataObject(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    codec: DataObjectCodec
    content_sha256: Sha256
    content_size: Annotated[int, Field(ge=0)]
    first_ms: Annotated[int | None, Field(ge=0)]
    last_ms: Annotated[int | None, Field(ge=0)]
    name: Annotated[str, Field(max_length=200, min_length=1)]
    role: DataObjectRole
    rows: Annotated[int, Field(ge=0)]
    rows_sha256: Sha256
    schema_id: Annotated[str, Field(max_length=80, min_length=1)]
    sha256: Sha256
    size: Annotated[int, Field(ge=0)]


class Difference(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    a: str | None
    b: str | None
    class_: Annotated[DifferenceClass, Field(alias="class")]
    delta: Dec | None
    event_ids: Annotated[list[Id], Field(max_length=8)]
    key: Annotated[str, Field(max_length=400, min_length=1)]
    layer: DifferenceLayer
    message: Text
    tolerance: UDec | None


class ErrorBody(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    code: Annotated[str, Field(max_length=80, min_length=3)]
    event: Annotated[str | None, Field(max_length=200)] = None
    field: Annotated[str | None, Field(max_length=400)] = None
    job_id: Id | None = None
    message: Annotated[str, Field(max_length=4000)]
    request_id: Id
    retryable: bool


class ErrorEnvelope(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    error: ErrorBody


class EventDataDecision(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    intents: Annotated[int, Field(ge=0)]
    note: Text | None = None
    state_sha256: Sha256 | None
    step_seq: Annotated[int | None, Field(ge=0)]


class EventDataEquity(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    by_ccy: dict[str, Dec]
    close_now_usdt: Dec | None
    fx: PDec | None
    stale: bool


class EventDataFee(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    amount: UDec
    ccy: Ccy
    fill_event_id: Id


class EventDataFill(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    fee: UDec
    fee_ccy: Ccy
    liquidity: EventDataFillLiquidity
    price: PDec
    qty: PDec
    side: EventDataIntentSide


class EventDataFunding(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    amount: Dec
    boundary_ms: Annotated[int, Field(ge=0)]
    ccy: Ccy
    position_qty: Dec
    rate: Dec
    rate_source: EventDataFundingRateSource
    settlement_price: PDec


class EventDataIntent(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    decision_time_ms: Annotated[int, Field(ge=0)]
    limit_price: PDec | None
    order_type: ExecutionProfileMatchingOrderTypesItem
    qty: PDec
    reduce_only: bool
    side: EventDataIntentSide


class EventDataOrderResult(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    adjustments: Annotated[list[Adjustment], Field(max_length=8)]
    effective_price: PDec | None
    effective_qty: UDec
    filled_qty: UDec
    reason: Text | None
    reason_code: str | None
    requested_price: PDec | None
    requested_qty: PDec
    status: EventDataOrderResultStatus


class EventDataPosition(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    avg_entry: PDec | None
    change: EventDataPositionChange
    qty: UDec
    side: EventDataPositionSide


class EventsHeader(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    count: Annotated[int, Field(ge=0)]
    run_id: Id
    schema_: Annotated[EventsHeaderSchema, Field(alias="schema")]


class ExecutionProfileAccount(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    currencies: Annotated[list[Ccy], Field(max_length=2, min_length=1)]
    leverage_perp: PDec
    leverage_spot: ExecutionProfileAccountLeverageSpot
    liquidation: ExecutionProfileAccountLiquidation
    margin_mode: ExecutionProfileAccountMarginMode
    position_mode: ExecutionProfileAccountPositionMode
    same_instant_margin: ExecutionProfileAccountSameInstantMargin | None = None
    starting_balance_per_venue: PDec


class ExecutionProfileDecisionClock(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    history_len: Annotated[int, Field(ge=0)]
    kind: ExecutionProfileDecisionClockKind


class ExecutionProfileEndOfRun(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    open_positions: ExecutionProfileEndOfRunOpenPositions


class ExecutionProfileFees(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    maker: ExecutionProfileFeesMaker
    model: ExecutionProfileFeesModel
    rounding: ExecutionProfileFeesRounding
    table_sha256: HashOrEmpty


class ExecutionProfileFunding(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    boundary_match: Annotated[str, Field(max_length=80, min_length=1)]
    mode: ExecutionProfileFundingMode
    notional: ExecutionProfileFundingNotional
    rounding: ExecutionProfileFundingRounding
    settlement_price: ExecutionProfileFundingSettlementPrice
    sign: ExecutionProfileFundingSign
    strict: bool


class ExecutionProfileFx(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    end_value: ExecutionProfileFxEndValue
    fill_time: ExecutionProfileFxFillTime
    pair: ExecutionProfileFxPair
    realized: ExecutionProfileFxRealized
    source: ExecutionProfileFxSource


class ExecutionProfileInitialState(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    cash: ExecutionProfileInitialStateCash
    positions: ExecutionProfileInitialStatePositions


class ExecutionProfileMatching(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    depth: ExecutionProfileMatchingDepth
    impact: ExecutionProfileMatchingDepth
    lot_step: ExecutionProfileMatchingDepth
    min_notional: ExecutionProfileMatchingDepth
    model: ExecutionProfileMatchingModel
    order_types: Annotated[
        list[ExecutionProfileMatchingOrderTypesItem], Field(max_length=4, min_length=1)
    ]
    queue: ExecutionProfileMatchingDepth
    quote_size: PDec
    reduce_only: bool
    submit_after_decision_ns: Annotated[int, Field(ge=1, le=1)]
    valuation_after_decision_ns: Annotated[int, Field(ge=2, le=2)]


class ExecutionProfilePrecision(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    money_unit: ExecutionProfilePrecisionMoneyUnit
    price: ExecutionProfilePrecisionPrice
    price_rounding: ExecutionProfilePrecisionPriceRounding
    quantity_rounding: ExecutionProfilePrecisionQuantityRounding
    size: ExecutionProfilePrecisionSize


class ExecutionProfileQuotes(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    decision_visibility: ExecutionProfileQuotesDecisionVisibility
    fill_future_tolerance_ms: Annotated[int, Field(ge=0)]
    max_age_ms: Annotated[int, Field(ge=0)]


class ExecutionProfileSampling(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    drawdown: ExecutionProfileSamplingDrawdown
    valuation: ExecutionProfileSamplingValuation


class Family(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    created_ms: int
    description: Text
    family_id: Id
    name: Annotated[str, Field(max_length=128, min_length=1)]
    vault_budget_remaining: Annotated[
        int,
        Field(
            description="Review runs this family may still be named by: 1 when registered, 0 once a review run used it.",
            ge=0,
            le=1,
        ),
    ]


class FamilyCreateRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    description: Text
    name: Annotated[
        str,
        Field(
            description="Name of the hypothesis family, unique per credential (1–128 characters). Registering the same name again with the\nsame description returns the existing family; a different description is `idempotency.conflict`.",
            max_length=128,
            min_length=1,
        ),
    ]


class InstrumentRef(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    market: Market
    symbol: Annotated[str, Field(max_length=96, min_length=1)]
    venue: Venue


class Intent(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    decision_time_ms: Annotated[int, Field(ge=0)]
    instrument: InstrumentRef
    intent_id: Id
    limit_price: PDec | None
    order_type: ExecutionProfileMatchingOrderTypesItem
    pair_id: Id | None = None
    position_ref: Id | None
    qty: PDec
    reduce_only: bool
    side: EventDataIntentSide


class Intents(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    intents: Annotated[list[Intent], Field(max_length=200000)]
    schema_: Annotated[IntentsSchema, Field(alias="schema")]


class Layer(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    reason: Text
    status: Status


class PackSelection2(BaseModel):
    pit_membership: PitMembership


class PackSelection(RootModel[PackSelection1 | PackSelection2]):
    root: Annotated[
        PackSelection1 | PackSelection2,
        Field(
            description="How a dataset pack's instrument list was chosen (see [`PackRequest::selection`])."
        ),
    ]


class PolicyContextLastResultsItem(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    avg_price: PDec | None
    filled_qty: UDec
    intent_id: Id
    reason_code: str | None
    status: EventDataOrderResultStatus


class PolicyContextPositionsItem(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    avg_entry: PDec
    instrument: InstrumentRef
    net_now: Dec | None
    position_ref: Id
    qty: PDec
    side: PolicyContextPositionsItemSide


class PolicyResponse(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    context_sha256: Sha256
    intents: Annotated[list[Intent], Field(max_length=1000)]
    pack_id: Id
    schema_: Annotated[PolicyResponseSchema, Field(alias="schema")]
    session_id: Id
    state_sha256: Sha256
    step_seq: Annotated[int, Field(ge=0)]
    strategy_content_sha256: Sha256


class ResultCapital(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    peak_margin_usdt: UDec | None


class ResultDrawdown(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    max: UDec | None
    points: Annotated[int, Field(ge=0)]


class ResultEventLog(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    count: Annotated[int, Field(ge=0)]
    name: Id
    sha256: Sha256


class ResultExecution(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    decision_blockers: Annotated[
        list[Text],
        Field(
            description="Every decision blocker, worded exactly as the console's fidelity result block words it (margin tiers that are a proxy\nor the unverified venue-wide table, a single assumed depth level, the minute-level stop/liquidation check that cannot\nrun, impact parameters that are assumed). Empty = nothing blocks.",
            max_length=40,
        ),
    ]
    decision_grade: Annotated[
        bool,
        Field(
            description="True when every decision blocker is waived (`profile.decision_grade_waivers`); false otherwise."
        ),
    ]
    decision_grade_waivers: Annotated[
        list[Text],
        Field(
            description="The blockers the request waived explicitly (`profile.decision_grade_waivers`, exact text). Default empty.",
            max_length=40,
        ),
    ]


class RunManifestOutputs(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    events_sha256: Sha256 | None
    facts_sha256: Sha256 | None
    result_sha256: Sha256 | None


class RunManifestScope(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    claims: Annotated[list[Text], Field(max_length=20)]
    compared: Annotated[list[Id], Field(max_length=10)]


class RunManifestStrategy(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    content_sha256: Sha256
    strategy_id: Id


class StrategyManifest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    content_sha256: Sha256
    contract: StrategyManifestContract
    dependency_lock_sha256: HashOrEmpty
    entry: StrategyManifestEntry
    files: Annotated[
        list[StrategyManifestFilesItem], Field(max_length=200, min_length=1)
    ]
    interpreter: Annotated[str, Field(max_length=120, min_length=1)]
    name: Annotated[str, Field(max_length=120, min_length=1)]
    parameters: dict[str, Any]
    requires: StrategyManifestRequires
    schema_: Annotated[StrategyManifestSchema, Field(alias="schema")]
    seed: Annotated[int | None, Field(ge=0)]
    strategy_id: Id
    supports: StrategyManifestSupports


class VerificationEvent(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    amount: Dec | None = None
    fee: Dec | None = None
    kind: VerificationEventKind
    long_entry: Dec | None = None
    long_exit: Dec | None = None
    net: Dec | None = None
    pair: Annotated[
        str,
        Field(
            description="The paper position id (`cross:<base>:<long leg>><short leg>`).",
            max_length=300,
            min_length=1,
        ),
    ]
    qty: Dec | None = None
    seq: Annotated[
        int,
        Field(
            description="Archive sequence number of the input frame the event happened at.",
            ge=0,
        ),
    ]
    short_entry: Dec | None = None
    short_exit: Dec | None = None
    ts_ms: Annotated[
        int | None,
        Field(description="Funding only: the settlement time, milliseconds."),
    ] = None


class ComparisonAffected(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    events: Annotated[int, Field(ge=0)]
    from_ms: Annotated[int | None, Field(ge=0)]
    instruments: Annotated[list[InstrumentRef], Field(max_length=200)]


class ComparisonLayers(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    L0: Layer
    L1: Layer
    L2: Layer
    L3: Layer
    L4: Layer


class DataLockRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    accept_partial: bool
    dataset: DatasetSpec | None = None
    funding_dataset: str | None
    include: DataLockRequestInclude
    instruments: list[InstrumentRef] | None
    venues: Annotated[list[Venue], Field(max_length=9, min_length=1)]
    warmup_frames: Annotated[int, Field(ge=0)]
    window: Window


class Event(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    data: dict[str, Any]
    event_id: Id
    instrument: InstrumentRef | None
    intent_id: Id | None
    kind: EventKind
    order_id: Id | None
    pair_id: Id | None
    position_ref: Id | None
    refs: Annotated[list[Id], Field(max_length=16)]
    run_id: Id
    seq: Annotated[int, Field(ge=0)]
    time_ms: Annotated[int, Field(ge=0)]
    time_ns: Ns


class ExecutionProfile(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    account: ExecutionProfileAccount
    decision_clock: ExecutionProfileDecisionClock
    end_of_run: ExecutionProfileEndOfRun
    engine: Annotated[str, Field(max_length=200, min_length=1)]
    event_order: ExecutionProfileEventOrder
    fees: ExecutionProfileFees
    fill: Annotated[
        dict[str, Any] | None,
        Field(
            description="Optional (2026-10-11): the fill model and capital cost an `ir_strategy` run used (`model`, `capital_cost_annual`,\nand for `conservative`: `spread_floor_bps`, `observed_spread_bps`, `impact_calibration`, `scenario`). When\npresent with `model = conservative` it supersedes `matching` for the main figures. Absent = one-level fills."
        ),
    ] = None
    funding: ExecutionProfileFunding
    fx: ExecutionProfileFx
    initial_state: ExecutionProfileInitialState
    matching: ExecutionProfileMatching
    precision: ExecutionProfilePrecision
    profile_id: Id
    quotes: ExecutionProfileQuotes | None = None
    sampling: ExecutionProfileSampling
    schema_: Annotated[ExecutionProfileSchema, Field(alias="schema")]
    unsupported: Annotated[list[Text], Field(max_length=40)]
    warmup: ExecutionProfileWarmup


class InputAccess(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    chain_sha256: Sha256
    first_ms: Annotated[int, Field(ge=0)]
    instrument: InstrumentRef | None
    last_ms: Annotated[int, Field(ge=0)]
    rows: Annotated[int, Field(ge=0)]
    table: InputAccessTable


class PackRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    accept_partial: bool | None = None
    dataset: DatasetSpec | None = None
    fees_override: Annotated[
        dict[str, FeeOverride] | None,
        Field(
            description="Scan packs only: venue -> taker fees, used for scan frames that recorded no fee schedule. Without it such\nframes reject the pack (the current account fees are never substituted). Recorded in the data lock and listed\nin the `approximations` of every run on the pack."
        ),
    ] = None
    funding_dataset: Annotated[
        str | None,
        Field(
            description='官方结算数据集版本；空 = 数据集可用时固定为当前最新（解析结果写进数据锁），`"none"` = 不含资金费结算。'
        ),
    ] = None
    include_native: bool | None = None
    include_tables: bool | None = None
    instruments: Annotated[
        list[PackInstrumentRef] | None,
        Field(
            description="显式筛选（记录在数据锁里）；空 = 不筛选，导出全部 universe。"
        ),
    ] = None
    selection: PackSelection | None = None
    venues: Annotated[list[str] | None, Field(description="空 = 全部九个平台。")] = None
    warmup_frames: Annotated[int | None, Field(ge=0)] = None
    window: WindowMs


class PolicyContext(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    accounts: Annotated[list[PolicyContextAccountsItem], Field(max_length=40)]
    data_refs: PolicyContextDataRefs
    history_len: Annotated[int, Field(ge=0)]
    last_results: Annotated[list[PolicyContextLastResultsItem], Field(max_length=2000)]
    positions: Annotated[list[PolicyContextPositionsItem], Field(max_length=2000)]
    schema_: Annotated[PolicyContextSchema, Field(alias="schema")]
    session_id: Id
    step_seq: Annotated[int, Field(ge=0)]
    time_ms: Annotated[int, Field(ge=0)]
    time_ns: Ns


class RunManifest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    builder: RunManifestBuilder
    created_ms: Annotated[int, Field(ge=0)]
    data_lock_sha256: Sha256
    engine: RunManifestEngine
    execution_profile_sha256: Sha256
    inputs: Annotated[list[InputAccess], Field(max_length=200000)]
    mode: Mode
    origin: RunManifestOrigin
    outputs: RunManifestOutputs
    pack_id: Id
    parameters: dict[str, Any]
    parent_run_id: Id | None
    run_id: Id
    schema_: Annotated[RunManifestSchema, Field(alias="schema")]
    scope: RunManifestScope
    seed: Annotated[int | None, Field(ge=0)]
    side: RunManifestSide
    status: RunManifestStatus
    strategy: RunManifestStrategy | None


class Verification(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    end_equity: VerificationEndEquity | None = None
    engine_recount: Status
    events: Annotated[
        list[VerificationEvent] | None,
        Field(
            description="Optional (protocol 2.4), scan-frame `ir_strategy` runs only: the IR decision ledger's events of the replay\n(opens, closes, funding settlements), each keyed by the input frame it happened at. AlphaKeel's G3 item 3.9\ncompares them one by one with the events of the strategy's paper journal."
        ),
    ] = None
    external_python: Status
    input_frames: InputFrames | None = None
    ir_ledger: Status | None = None
    ir_ledger_problems: Annotated[
        dict[str, Any] | None,
        Field(
            description="`ir_strategy` with `ir_ledger = mismatch` only: why (the engine-vs-ledger audit: maximum differences and the\nfirst problems). Absent otherwise."
        ),
    ] = None
    native_rule_ledger: Status


class Comparison(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    a: Id
    affected: ComparisonAffected
    b: Id
    comparison_id: Id
    created_ms: Annotated[int, Field(ge=0)]
    differences: Annotated[list[Difference], Field(max_length=200)]
    first_difference: Difference | None
    layers: ComparisonLayers
    mode: Mode
    notes: Annotated[list[Text], Field(max_length=40)]
    pass_scope: Annotated[str, Field(max_length=600)]
    passed: bool
    required_checks: Annotated[list[Check], Field(max_length=40, min_length=1)]
    schema_: Annotated[ComparisonSchema, Field(alias="schema")]


class DataLock(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    coverage: DataLockCoverage
    created_ms: Annotated[int, Field(ge=0)]
    datasets: Annotated[
        list[DataLockDatasetsItem] | None,
        Field(
            description="Versioned datasets pinned at acceptance (dataset packs only).",
            max_length=8,
        ),
    ] = None
    fees_override: DataLockFeesOverride | None = None
    funding: DataLockFunding | None
    limits: Annotated[list[Text], Field(max_length=100)]
    objects: Annotated[list[DataObject], Field(max_length=100000, min_length=1)]
    pack_id: Id
    pack_sha256: Sha256
    request: DataLockRequest
    scans: DataLockScans | None
    schema_: Annotated[DataLockSchema, Field(alias="schema")]
    sources: Annotated[list[DataLockSourcesItem], Field(max_length=64, min_length=1)]
    transforms: Annotated[list[DataLockTransformsItem], Field(max_length=64)]
    units: DataLockUnits
    universe: DataLockUniverse
    windows: DataLockWindows


class RunResult(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    amounts: ResultAmounts
    amounts_ok: bool | None
    approximations: Annotated[
        list[Text] | None,
        Field(
            description="Optional (2026-10-11), `ir_strategy` only: the assumptions behind the fill model (`synthetic_spread_floor`,\n`sqrt_impact_modeled_depth`, `lot_step_inferred_from_archive` / `instrument_meta_rules`, `leg_failure_assumed`,\n`decisions_with_conservative_feedback` (until 2026-10-13 `decisions_from_top_of_book_ledger`), `top_of_book_full_fill`;\nsince 2026-10-17 `maker_fill_model_M1` / `maker_fill_model_M2`, `maker_hedge_pricing` for an IR with a maker entry leg on a\nconservative run and `maker_not_modelled` on a one-level run of such an IR), each starting with its identifier and a colon.",
            max_length=40,
        ),
    ] = None
    by_currency: dict[str, ResultByCurrencyValue]
    capital: ResultCapital
    capital_cost: Annotated[
        dict[str, Any] | None,
        Field(
            description="Optional (2026-10-11): capital-occupation cost (idle margin of both legs x `annual_rate` x holding time), kept\napart from fees and deducted in `amounts.net_of_capital_cost`."
        ),
    ] = None
    counts: BTreeMap
    daily_equity: Annotated[
        list[DailyEquityPoint] | None,
        Field(
            description="Optional (protocol 2.4): the run's net (close-now value, equity minus start) at the last valued point of every UTC day,\nascending by day; net of the capital cost accrued up to the day's close when the run charges one (the same basis as `amounts.net_of_capital_cost`). AlphaKeel recomputes the G1 daily returns (1.6 DSR, 1.8 bootstrap) from it."
        ),
    ] = None
    data_quality: BTreeMap
    dataset_view: Annotated[
        dict[str, Any] | None,
        Field(
            description="`ir_strategy` on a dataset pack only: the `dataset_view` request, instants, instants without data and the\nview's approximations."
        ),
    ] = None
    drawdown: ResultDrawdown
    equity: ResultEquity
    event_log: ResultEventLog
    execution: ResultExecution | None = None
    execution_ok: bool | None
    fill_model: ResultFillModel | None = None
    limitations: Annotated[list[Text], Field(max_length=40)]
    mode: Mode
    not_applicable_metrics: Annotated[list[Id], Field(max_length=40)]
    optimistic: Annotated[
        dict[str, Any] | None,
        Field(
            description="Optional (2026-10-11), conservative `ir_strategy` runs only: the same run under one-level full fills\n(`fill_model`, `execution_ok`, `amounts_ok`, `amounts`, `by_currency`, `equity`, `drawdown`, `capital`,\n`counts`, `event_log`, `problems`, `events_sha256`). The main figures are the conservative ones."
        ),
    ] = None
    problems: Annotated[list[Text], Field(max_length=50)]
    run_assumptions: Annotated[
        dict[str, Any] | None,
        Field(
            description="Optional (2026-10-11), `ir_strategy` runs: the assumptions the run was made under (`fill_model`, `spread_floor_bps`,\n`capital_cost_annual`, `realism`, `runtime_guards`, `margin_tiers`, and since protocol 2.3 `fees_perp_taker` (venue ->\nperp taker the run actually used), `leverage_perp`, `funding_mode`; since 2026-10-12 `decision_grade_waivers`; since 2026-10-17\n`fees_spot_taker` (venue -> spot taker, spot_perp IRs only) and `maker` (conservative runs of an IR whose `execution` posts an\nentry leg maker: `{model, leg, timeout_s, participation, chase, exit, fees_maker: {venue: {perp|spot: rate}}, fills: {candidates,\nposted, posted_qty, maker_qty, chased, chased_qty}}`)). The AlphaKeel verdict reads it (items 1.14 and the\nbinding of the robustness evidence). Only runs of the service's own engine carry it: a registered external run\n(`POST /runs/external`) that includes `run_assumptions`, `fill_model`, `capital_cost`, `optimistic` or `execution` is refused."
        ),
    ] = None
    run_id: Id
    schema_: Annotated[ResultSchema, Field(alias="schema")]
    side: RunManifestSide
    strategy_id: Id | None = None
    strategy_sha256: Sha256 | None = None
    verification: Verification
    window: ResultWindow | None = None
