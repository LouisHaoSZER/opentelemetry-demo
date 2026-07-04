#!/usr/bin/python

# Copyright The OpenTelemetry Authors
# SPDX-License-Identifier: Apache-2.0
#
# =============================================================================
# load-generator · locustfile
#
# 设计原则（对齐 混沌工程-稳态流量基线设计文档 v2）:
#   1. 主力为 API 接口打流（WebsiteUser / HttpUser），浏览器流量默认关闭。
#   2. 浏览器流量（WebsiteBrowserUser / PlaywrightUser）走 e2b 远程沙箱 CDP，
#      通过 LOCUST_BROWSER_TRAFFIC_ENABLED=true 显式打开，仅用于调试阶段。
#   3. 压测阶段全部使用 HttpUser API 调用，浏览器不做稳态流量贡献。
#
# 环境变量:
#   LOCUST_BROWSER_TRAFFIC_ENABLED   是否启用浏览器流量, 默认 false
#   E2B_API_KEY / E2B_DOMAIN / E2B_TEMPLATE   浏览器流量时需要
#   FLAGD_HOST / FLAGD_OFREP_PORT   flagd 连接信息
#   OTEL_SDK_DISABLED               是否禁用 OTel 导出, 本地默认 true
# =============================================================================

import json
import logging
import os
import random
import sys
import uuid

from locust import HttpUser, between, task
from openfeature import api
from openfeature.contrib.hook.opentelemetry import TracingHook
from openfeature.contrib.provider.ofrep import OFREPProvider
from opentelemetry import baggage, context, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.jinja2 import Jinja2Instrumentor
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.instrumentation.system_metrics import SystemMetricsInstrumentor
from opentelemetry.instrumentation.urllib3 import URLLib3Instrumentor
from opentelemetry.metrics import set_meter_provider
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

# =============================================================================
# Section 1: OpenTelemetry 初始化 (trace / log / metric)
# =============================================================================

tracer_provider = TracerProvider()
trace.set_tracer_provider(tracer_provider)
tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(insecure=True)))

logger_provider = LoggerProvider()
set_logger_provider(logger_provider)

log_exporter = OTLPLogExporter(insecure=True)
logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))

handler = LoggingHandler(level=logging.INFO, logger_provider=logger_provider)
stdout_handler = logging.StreamHandler(sys.stdout)
stdout_handler.setLevel(logging.INFO)
stdout_handler.setFormatter(
    logging.Formatter(
        fmt="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
)

root_logger = logging.getLogger()
root_logger.addHandler(handler)
root_logger.addHandler(stdout_handler)
root_logger.setLevel(logging.INFO)

metric_exporter = OTLPMetricExporter(insecure=True)
set_meter_provider(MeterProvider([PeriodicExportingMetricReader(metric_exporter)]))

LoggingInstrumentor().instrument(set_logging_format=True)
Jinja2Instrumentor().instrument()
RequestsInstrumentor().instrument()
SystemMetricsInstrumentor().instrument()
URLLib3Instrumentor().instrument()

logging.info("Instrumentation complete - logs will now include trace context")

# =============================================================================
# Section 2: Flagd (OpenFeature) 初始化
# =============================================================================

base_url = (
    f"http://{os.environ.get('FLAGD_HOST', 'localhost')}:"
    f"{os.environ.get('FLAGD_OFREP_PORT', 8016)}"
)
api.set_provider(OFREPProvider(base_url=base_url))
api.add_hooks([TracingHook()])


def get_flagd_value(FlagName):
    client = api.get_client()
    return client.get_integer_value(FlagName, 0)


# =============================================================================
# Section 3: 共享数据 (产品 / 分类 / 用户)
# =============================================================================

categories = [
    "binoculars",
    "telescopes",
    "accessories",
    "assembly",
    "travel",
    "books",
    None,
]

products = [
    "0PUK6V6EV0",
    "1YMWWN1N4O",
    "2ZYFJ3GM2N",
    "66VCHSJNUP",
    "6E92ZMYYFZ",
    "9SIQT8TOJO",
    "L9ECAV7KIM",
    "LS4PSXUNUM",
    "OLJCESPC7Z",
    "HQTGWGPNH4",
]

people_file = open("people.json")
people = json.load(people_file)

# =============================================================================
# Section 4: WebsiteUser — 主力 API 打流 (HttpUser)
#
# 稳定的 HttpUser，通过 @task 权重覆盖所有核心 API:
#   - 浏览商品/详情     browse_product          (task 权重 10)
#   - 首页洪泛控制      flood_home              (task 权重 5, 由 flagd 控制)
#   - 推荐/广告/评价     get_recommendations / get_ads / get_product_reviews (3+3+2)
#   - 购物车操作        view_cart / add_to_cart (3+2)
#   - 结账              checkout / checkout_multi (1+1)
#   - AI 助手            ask_product_ai_assistant (1)
#   - 首页               index                   (1)
#
# 并发数不变: 当前 task 权重分布保持原样，仅关闭浏览器流量作为默认行为。
# =============================================================================

class WebsiteUser(HttpUser):
    wait_time = between(1, 10)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.tracer = trace.get_tracer(__name__)

    @task(6)
    def index(self):
        with self.tracer.start_as_current_span("user_index", context=Context()):
            logging.info("User accessing index page")
            self.client.get("/")

    @task(20)
    def browse_product(self):
        product = random.choice(products)
        with self.tracer.start_as_current_span(
            "user_browse_product", context=Context(), attributes={"product.id": product}
        ):
            logging.info(f"User browsing product: {product}")
            self.client.get("/api/products/" + product)

    @task(18)
    def get_recommendations(self):
        product = random.choice(products)
        with self.tracer.start_as_current_span(
            "user_get_recommendations",
            context=Context(),
            attributes={"product.id": product},
        ):
            logging.info(f"User getting recommendations for product: {product}")
            params = {
                "productIds": [product],
            }
            self.client.get("/api/recommendations", params=params)

    @task(10)
    def get_product_reviews(self):
        product = random.choice(products)
        with self.tracer.start_as_current_span(
            "user_get_product_reviews",
            context=Context(),
            attributes={"product.id": product},
        ):
            logging.info(f"User getting product reviews for product: {product}")
            self.client.get("/api/product-reviews/" + product)

    @task(3)
    def ask_product_ai_assistant(self):
        product = random.choice(products)
        question = "Can you summarize the product reviews?"
        with self.tracer.start_as_current_span(
            "user_ask_product_ai_assistant",
            context=Context(),
            attributes={"product.id": product, "question": question},
        ):
            logging.info(
                f"Asking the AI Assistant a question for: {product} {question}"
            )
            question = {"question": question}
            self.client.post("/api/product-ask-ai-assistant/" + product, json=question)

    @task(13)
    def get_ads(self):
        category = random.choice(categories)
        with self.tracer.start_as_current_span(
            "user_get_ads", context=Context(), attributes={"category": str(category)}
        ):
            logging.info(f"User getting ads for category: {category}")
            params = {
                "contextKeys": [category],
            }
            self.client.get("/api/data/", params=params)

    @task(18)
    def view_cart(self):
        with self.tracer.start_as_current_span("user_view_cart", context=Context()):
            logging.info("User viewing cart")
            self.client.get("/api/cart")

    @task(4)  # 翻倍，加购率 ~4.3%
    def add_to_cart(self, user=""):
        if user == "":
            user = str(uuid.uuid1())
        product = random.choice(products)
        quantity = random.choice([1, 2, 3, 4, 5, 10])
        with self.tracer.start_as_current_span(
            "user_add_to_cart",
            context=Context(),
            attributes={"user.id": user, "product.id": product, "quantity": quantity},
        ):
            logging.info(f"User {user} adding {quantity} of product {product} to cart")
            self.client.get("/api/products/" + product)
            cart_item = {
                "item": {
                    "productId": product,
                    "quantity": quantity,
                },
                "userId": user,
            }
            self.client.post("/api/cart", json=cart_item)

    @task(2)  # 翻倍，结账率 ~2.2%
    def checkout(self):
        user = str(uuid.uuid1())
        with self.tracer.start_as_current_span(
            "user_checkout_single", context=Context(), attributes={"user.id": user}
        ):
            self.add_to_cart(user=user)
            checkout_person = random.choice(people)
            checkout_person["userId"] = user
            self.client.post("/api/checkout", json=checkout_person)
            logging.info(f"Checkout completed for user {user}")

    @task(0)  # 并入 checkout，避免重复计算 checkout 漏斗
    def checkout_multi(self):
        user = str(uuid.uuid1())
        item_count = random.choice([2, 3, 4])
        with self.tracer.start_as_current_span(
            "user_checkout_multi",
            context=Context(),
            attributes={"user.id": user, "item.count": item_count},
        ):
            for i in range(item_count):
                self.add_to_cart(user=user)
            checkout_person = random.choice(people)
            checkout_person["userId"] = user
            self.client.post("/api/checkout", json=checkout_person)
            logging.info(f"Multi-item checkout completed for user {user}")

    @task(0)  # 稳态基线必须为 0，归类为 flagd 故障注入项
    def flood_home(self):
        flood_count = get_flagd_value("loadGeneratorFloodHomepage")
        if flood_count > 0:
            with self.tracer.start_as_current_span(
                "user_flood_home",
                context=Context(),
                attributes={"flood.count": flood_count},
            ):
                logging.info(f"User flooding homepage {flood_count} times")
                for _ in range(0, flood_count):
                    self.client.get("/")

    def on_start(self):
        with self.tracer.start_as_current_span("user_session_start", context=Context()):
            session_id = str(uuid.uuid4())
            logging.info(f"Starting user session: {session_id}")
            ctx = baggage.set_baggage("session.id", session_id)
            ctx = baggage.set_baggage("synthetic_request", "true", context=ctx)
            context.attach(ctx)
            self.index()


# =============================================================================
# Section 5: WebsiteBrowserUser — 浏览器流量 (可选, 默认关闭)
#
# 浏览器流量走 e2b 远程沙箱 CDP, 本地不跑 Chromium。
# 打开方式: LOCUST_BROWSER_TRAFFIC_ENABLED=true
#
# 设计文档 v2 §6.4 明确: 稳态基线不依赖浏览器流量 (成本+配额+带宽约束),
# 浏览器流量仅用于前期调试/验证阶段。
# =============================================================================

browser_traffic_enabled = os.environ.get(
    "LOCUST_BROWSER_TRAFFIC_ENABLED", "false"
).lower() in ("true", "yes", "on")

if not browser_traffic_enabled:
    logging.info(
        "Browser traffic DISABLED (LOCUST_BROWSER_TRAFFIC_ENABLED=false). "
        "Only WebsiteUser (HttpUser) will generate traffic."
    )
else:
    # 延迟导入: 仅浏览器模式需要 e2b + playwright
    from e2b import Sandbox  # type: ignore
    from playwright.async_api import Request, Route, async_playwright
    from locust_plugins.users.playwright import PageWithRetry, PlaywrightUser

    E2B_TEMPLATE = os.environ.get("E2B_TEMPLATE", "ai-demo-browser")
    E2B_CDP_PORT = int(os.environ.get("E2B_CDP_PORT", "9000"))
    _e2b_sandbox_timeout_env = os.environ.get("E2B_SANDBOX_TIMEOUT")
    E2B_SANDBOX_TIMEOUT: int | None = (
        int(_e2b_sandbox_timeout_env) if _e2b_sandbox_timeout_env else None
    )

    logging.info(
        f"Browser traffic ENABLED. "
        f"e2b template={E2B_TEMPLATE}, cdp_port={E2B_CDP_PORT}"
    )

    # -------------------------------------------------------------------------
    # 5.1 pw_resilient: 浏览器 task 装饰器, 前置沙箱自检/重建
    # -------------------------------------------------------------------------
    def pw_resilient(func):
        """替代 @pw 的装饰器: 入口先调 _pwprep 走自检/重建沙箱, 再创建
        context+page 并执行 task。复刻 locust-plugins.users.playwright.pw,
        仅前置一次 _pwprep 处理远端沙箱被 AGS 回收后 self.browser 失活的情况。"""
        from locust_plugins.users.playwright import sync

        @sync
        async def pwwrapFunc(user: "PlaywrightUser"):
            await user._pwprep()  # type: ignore[attr-defined]
            if user.browser_context:
                await user.browser_context.close()
            user.browser_context = await user.browser.new_context(
                ignore_https_errors=True, base_url=user.host
            )
            user.page = await user.browser_context.new_page()  # type: ignore[assignment]
            await func(user, user.page)

        return pwwrapFunc

    # -------------------------------------------------------------------------
    # 5.2 WebsiteBrowserUser: Playwright 通过 CDP 连远端 e2b 沙箱
    # -------------------------------------------------------------------------
    class WebsiteBrowserUser(PlaywrightUser):
        fixed_count = int(os.environ.get("LOCUST_BROWSER_FIXED_COUNT", "5"))
        headless = True  # type: ignore  # CDP 模式 headless 由沙箱决定

        async def _provision_sandbox_browser(self):
            """创建 e2b 沙箱并通过 connect_over_cdp 设置 self.browser。"""
            logging.info(
                f"Creating e2b browser sandbox (template={E2B_TEMPLATE}, "
                f"timeout={E2B_SANDBOX_TIMEOUT or 'AGS-default(5m)'})"
            )
            create_kwargs = {"template": E2B_TEMPLATE}
            if E2B_SANDBOX_TIMEOUT is not None:
                create_kwargs["timeout"] = E2B_SANDBOX_TIMEOUT  # type: ignore[assignment]
            self._sandbox = Sandbox.create(**create_kwargs)
            access_token = self._sandbox._envd_access_token
            cdp_host = self._sandbox.get_host(E2B_CDP_PORT)
            cdp_url = f"https://{cdp_host}/cdp?access_token={access_token}"
            live_url = (
                f"https://{cdp_host}/novnc/vnc_lite.html"
                f"?access_token={access_token}"
                f"&path=websockify%3Faccess_token%3D{access_token}"
            )
            logging.info(f"Connecting Playwright over CDP: {cdp_host}")
            logging.info(f"Sandbox LIVE_URL (open in browser): {live_url}")
            self.browser = await self.playwright.chromium.connect_over_cdp(
                cdp_url,
                headers={"X-Access-Token": str(access_token)},
            )
            logging.info("CDP connection established")

        async def _pwprep(self):  # type: ignore[override]
            """覆写父类: e2b 沙箱 + CDP 替代本地 chromium.launch()。
            入口自检 is_connected, 失活则重建沙箱 + 清空 context/page 句柄。"""
            if self.playwright is None:
                self.playwright = await async_playwright().start()

            if self.browser is not None and not self.browser.is_connected():
                logging.warning(
                    "browser CDP disconnected (sandbox likely recycled), "
                    "rebuilding sandbox + reconnecting"
                )
                old = getattr(self, "_sandbox", None)
                if old is not None:
                    try:
                        old.kill()
                    except Exception as e:
                        logging.warning(f"old sandbox.kill failed (ignored): {e}")
                    self._sandbox = None
                self.browser = None  # type: ignore[assignment]
                self.browser_context = None  # type: ignore[assignment]
                self.page = None  # type: ignore[assignment]

            if self.browser is None:
                await self._provision_sandbox_browser()

        def on_stop(self):  # type: ignore[override]
            """user 停止时主动 kill 远端沙箱, 防计费泄漏。"""
            sandbox = getattr(self, "_sandbox", None)
            if sandbox is not None:
                try:
                    sandbox.kill()
                    logging.info("e2b sandbox killed")
                except Exception as e:
                    logging.error(f"Error killing e2b sandbox: {e}")
                finally:
                    self._sandbox = None

        async def _prepare_page(self, page: PageWithRetry):
            page.on("console", lambda msg: print(msg.text))
            _attach_requestfailed_logger(page)
            await page.route("**/*", add_baggage_header)

        # ---------------------------------------------------------------------
        # 5.2.1 浏览器 task 辅助方法
        # ---------------------------------------------------------------------
        async def _open_roof_binoculars(self, page: PageWithRetry):
            await page.goto("/", wait_until="commit")
            await page.wait_for_event(
                "response",
                predicate=lambda r: (
                    "/images/products/RoofBinoculars.jpg" in r.url and r.status == 200
                ),
                timeout=30000,
            )
            await page.click('p:has-text("Roof Binoculars")')
            await page.wait_for_selector(
                '[data-cy="product-add-to-cart"]', timeout=30000
            )

        async def _add_roof_binoculars_to_cart(self, page: PageWithRetry):
            await self._open_roof_binoculars(page)
            async with page.expect_response(
                lambda r: "/api/cart" in r.url and r.request.method == "POST",
                timeout=30000,
            ) as add_cart_response:
                await page.click('[data-cy="product-add-to-cart"]')
            response = await add_cart_response.value
            logging.info(f"Browser add-to-cart response status={response.status}")
            await page.wait_for_url("**/cart", timeout=30000)
            await page.wait_for_selector(
                '[data-cy="checkout-place-order"]', timeout=30000
            )

        async def _fill_international_checkout_form(self, page: PageWithRetry):
            await page.fill("#email", "browser-checkout@example.com")
            await page.fill("#street_address", "150 Elgin St")
            await page.fill("#zip_code", "K2P1L4")
            await page.fill("#city", "Ottawa")
            await page.fill("#state", "ON")
            await page.fill("#country", "Canada")
            await page.fill("#credit_card_number", "4432-8015-6152-0454")
            await page.select_option("#credit_card_expiration_month", "1")
            await page.select_option("#credit_card_expiration_year", "2030")
            await page.fill("#credit_card_cvv", "672")

        # ---------------------------------------------------------------------
        # 5.2.2 浏览器 task 任务
        # ---------------------------------------------------------------------
        @task  # type: ignore
        @pw_resilient
        async def open_cart_page_and_change_currency(self, page: PageWithRetry):
            tracer = trace.get_tracer(__name__)
            with tracer.start_as_current_span(
                "browser_change_currency", context=Context()
            ):
                try:
                    await self._prepare_page(page)
                    await page.goto("/cart", wait_until="commit")
                    await page.wait_for_selector(
                        '[name="currency_code"]', timeout=15000
                    )
                    await page.select_option('[name="currency_code"]', "CHF")
                    await page.wait_for_timeout(2000)
                    logging.info("Currency changed to CHF")
                except Exception as e:
                    logging.error(f"Error in change currency task: {str(e)}")

        @task  # type: ignore
        @pw_resilient
        async def add_product_to_cart(self, page: PageWithRetry):
            tracer = trace.get_tracer(__name__)
            with tracer.start_as_current_span("browser_add_to_cart", context=Context()):
                try:
                    await self._prepare_page(page)
                    await self._add_roof_binoculars_to_cart(page)
                    await page.wait_for_timeout(2000)
                    logging.info("Product added to cart successfully")
                except Exception as e:
                    logging.error(f"Error in add to cart task: {str(e)}")

        @task  # type: ignore
        @pw_resilient
        async def browse_product_detail_and_ask_ai(self, page: PageWithRetry):
            tracer = trace.get_tracer(__name__)
            with tracer.start_as_current_span("browser_product_ai", context=Context()):
                try:
                    await self._prepare_page(page)
                    await page.goto("/product/2ZYFJ3GM2N", wait_until="commit")
                    await page.wait_for_selector(
                        '[data-cy="product-add-to-cart"]', timeout=30000
                    )
                    await page.wait_for_selector(
                        '[data-cy="product-reviews"]', timeout=30000
                    )
                    async with page.expect_response(
                        lambda r: "/api/product-ask-ai-assistant/2ZYFJ3GM2N" in r.url
                        and r.request.method == "POST",
                        timeout=60000,
                    ) as ai_response:
                        await page.click('[data-cy="QuickPromptSummarize"]')
                    response = await ai_response.value
                    logging.info(f"AI summary response status={response.status}")
                    await page.wait_for_timeout(2000)
                except Exception as e:
                    logging.error(f"Error in product AI task: {str(e)}")

        @task  # type: ignore
        @pw_resilient
        async def add_product_to_cart_and_empty_cart(self, page: PageWithRetry):
            tracer = trace.get_tracer(__name__)
            with tracer.start_as_current_span("browser_empty_cart", context=Context()):
                try:
                    await self._prepare_page(page)
                    await self._add_roof_binoculars_to_cart(page)
                    async with page.expect_response(
                        lambda r: "/api/cart" in r.url and r.request.method == "DELETE",
                        timeout=30000,
                    ) as empty_cart_response:
                        await page.click('button:has-text("Empty Cart")')
                    response = await empty_cart_response.value
                    logging.info(f"Browser empty-cart response status={response.status}")
                    await page.wait_for_timeout(2000)
                except Exception as e:
                    logging.error(f"Error in empty cart task: {str(e)}")

        @task  # type: ignore
        @pw_resilient
        async def add_product_to_cart_and_checkout(self, page: PageWithRetry):
            tracer = trace.get_tracer(__name__)
            with tracer.start_as_current_span("browser_checkout_order", context=Context()):
                try:
                    await self._prepare_page(page)
                    await self._add_roof_binoculars_to_cart(page)
                    await self._fill_international_checkout_form(page)
                    async with page.expect_response(
                        lambda r: "/api/checkout" in r.url
                        and r.request.method == "POST",
                        timeout=90000,
                    ) as checkout_response:
                        await page.click('[data-cy="checkout-place-order"]')
                    response = await checkout_response.value
                    logging.info(f"Browser checkout response status={response.status}")
                    if response.status < 400:
                        await page.wait_for_url("**/checkout/**", timeout=60000)
                        await page.wait_for_selector(
                            'text="Your order is complete!"', timeout=30000
                        )
                    await page.wait_for_timeout(2000)
                except Exception as e:
                    logging.error(f"Error in checkout task: {str(e)}")

    # -------------------------------------------------------------------------
    # 5.3 浏览器辅助函数
    # -------------------------------------------------------------------------
    _log_failed_requests = os.environ.get(
        "LOCUST_BROWSER_LOG_FAILED_REQUESTS", "true"
    ).lower() in ("true", "yes", "on", "1")


    def _attach_requestfailed_logger(page):
        """浏览器 task 中 page.on("console") 收到的失败请求消息不带 URL,
        这里挂 requestfailed 事件把 "失败 URL + 失败原因" 打到日志便于排查。
        通过 LOCUST_BROWSER_LOG_FAILED_REQUESTS=false 关闭。"""
        if not _log_failed_requests:
            return

        def _log(req):
            try:
                failure = req.failure or "<unknown>"
                logging.info(f"[browser/requestfailed] {failure} {req.url}")
            except Exception:
                pass

        page.on("requestfailed", _log)


    async def add_baggage_header(route: Route, request: Request):
        """给浏览器发出的请求注入 baggage: synthetic_request=true,
        用于在后端 metrics 上区分合成流量与真实用户流量。"""
        existing_baggage = request.headers.get("baggage", "")
        headers = {
            **request.headers,
            "baggage": ", ".join(
                filter(None, (existing_baggage, "synthetic_request=true"))
            ),
        }
        await route.continue_(headers=headers)
