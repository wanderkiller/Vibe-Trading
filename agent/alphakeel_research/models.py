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
        Field(description="Subset of `kline_1m`, `bbo`, `depth20`, `instrument_meta`."),
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


class IntentsSchema(StrEnum):
    alphakeel_intents_1 = "alphakeel.intents/1"


class Market(StrEnum):
    perp = "perp"
    spot = "spot"


class Mode(StrEnum):
    native_strategy = "native_strategy"
    fixed_intent_replay = "fixed_intent_replay"
    python_policy = "python_policy"


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


class ResultSchema(StrEnum):
    alphakeel_result_1 = "alphakeel.result/1"


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


class Verification(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    engine_recount: Status
    external_python: Status
    native_rule_ledger: Status


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
    last_ms: Annotated[int, Field(ge=0)]
    manifest_sha256: Sha256
    max_gap_ms: Annotated[int, Field(ge=0)]
    start_seq: Annotated[int, Field(ge=0)]
    venue_failures: BTreeMap


class DataLockSourcesItem(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
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


class ExecutionProfileSampling(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    drawdown: ExecutionProfileSamplingDrawdown
    valuation: ExecutionProfileSamplingValuation


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


class PackRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    accept_partial: bool | None = None
    dataset: DatasetSpec | None = None
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
    venues: Annotated[list[str] | None, Field(description="空 = 全部九个平台。")] = None
    warmup_frames: Annotated[int | None, Field(ge=0)] = None
    window: WindowMs


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


class RunResult(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    amounts: ResultAmounts
    amounts_ok: bool | None
    by_currency: dict[str, ResultByCurrencyValue]
    capital: ResultCapital
    counts: BTreeMap
    data_quality: BTreeMap
    drawdown: ResultDrawdown
    equity: ResultEquity
    event_log: ResultEventLog
    execution_ok: bool | None
    limitations: Annotated[list[Text], Field(max_length=40)]
    mode: Mode
    not_applicable_metrics: Annotated[list[Id], Field(max_length=40)]
    problems: Annotated[list[Text], Field(max_length=50)]
    run_id: Id
    schema_: Annotated[ResultSchema, Field(alias="schema")]
    side: RunManifestSide
    verification: Verification


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
    funding: ExecutionProfileFunding
    fx: ExecutionProfileFx
    initial_state: ExecutionProfileInitialState
    matching: ExecutionProfileMatching
    precision: ExecutionProfilePrecision
    profile_id: Id
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
