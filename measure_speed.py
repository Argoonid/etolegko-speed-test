import argparse
from dataclasses import asdict, dataclass
import http.client
import json
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_REQUESTS = 10
DEFAULT_TIMEOUT = 30.0
CHUNK_SIZE = 262_144
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; net-speed-check/1.0)",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Accept-Encoding": "identity",
}


class DownloadError(Exception):
    pass


class IncompleteDownloadError(DownloadError):
    pass


class EmptyResponseError(DownloadError):
    pass


@dataclass(frozen=True)
class RequestSample:
    index: int
    bytes_read: int
    elapsed_s: float
    ttfb_s: float


@dataclass(frozen=True)
class BenchmarkResult:
    url: str
    requests_count: int
    total_bytes: int
    total_time_s: float
    avg_time_s: float
    median_time_s: float
    min_time_s: float
    max_time_s: float
    avg_ttfb_s: float
    speed_mbps: float
    speed_mb_s: float
    samples: list[RequestSample]


def validate_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"Ожидается корректный HTTP/HTTPS URL, получено: {url!r}")
    return url


def _parse_content_length(header_value: str | None) -> int | None:
    if not header_value:
        return None
    try:
        value = int(header_value.strip())
    except ValueError:
        return None
    return value if value >= 0 else None


def fetch_once(
    url: str,
    timeout: float,
    index: int = 1,
    buf: bytearray | None = None,
) -> RequestSample:
    req = urllib.request.Request(url, headers=HEADERS, method="GET")
    buffer = buf if buf is not None else bytearray(CHUNK_SIZE)
    started = time.perf_counter()
    downloaded = 0

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ttfb = time.perf_counter() - started
            expected = _parse_content_length(resp.headers.get("Content-Length"))

            while n := resp.readinto(buffer):
                downloaded += n
    except http.client.IncompleteRead as err:
        raise IncompleteDownloadError(
            f"Соединение оборвано на {len(err.partial)} Б"
        ) from err

    elapsed = time.perf_counter() - started

    if expected is not None and downloaded < expected:
        raise IncompleteDownloadError(
            f"Неполный ответ: получено {downloaded} Б из {expected} Б"
        )
    if downloaded == 0:
        raise EmptyResponseError("Сервер вернул пустое тело ответа (0 Б)")

    return RequestSample(
        index=index,
        bytes_read=downloaded,
        elapsed_s=elapsed,
        ttfb_s=ttfb,
    )


def run_benchmark(
    url: str,
    count: int = DEFAULT_REQUESTS,
    timeout: float = DEFAULT_TIMEOUT,
    on_progress=None,
) -> BenchmarkResult:
    validate_url(url)
    if count < 1:
        raise ValueError("Количество запросов должно быть больше 0")
    if timeout <= 0:
        raise ValueError("Тайм-аут должен быть положительным числом")

    shared_buf = bytearray(CHUNK_SIZE)
    samples: list[RequestSample] = []

    for idx in range(1, count + 1):
        sample = fetch_once(url, timeout=timeout, index=idx, buf=shared_buf)
        samples.append(sample)
        if on_progress:
            on_progress(sample, count)

    times = [s.elapsed_s for s in samples]
    total_bytes = sum(s.bytes_read for s in samples)
    total_time = sum(times)
    bytes_per_sec = total_bytes / total_time if total_time > 0 else 0.0

    return BenchmarkResult(
        url=url,
        requests_count=count,
        total_bytes=total_bytes,
        total_time_s=total_time,
        avg_time_s=total_time / count,
        median_time_s=statistics.median(times),
        min_time_s=min(times),
        max_time_s=max(times),
        avg_ttfb_s=sum(s.ttfb_s for s in samples) / count,
        speed_mbps=(bytes_per_sec * 8) / 1_000_000,
        speed_mb_s=bytes_per_sec / 1_000_000,
        samples=samples,
    )


def format_size(num_bytes: int) -> str:
    if num_bytes >= 1_000_000:
        return f"{num_bytes / 1_000_000:.2f} MB ({num_bytes:,} B)"
    if num_bytes >= 1_000:
        return f"{num_bytes / 1_000:.2f} KB ({num_bytes:,} B)"
    return f"{num_bytes} B"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Последовательный замер скорости скачивания по указанному URL."
    )
    parser.add_argument("url", help="HTTP/HTTPS адрес ресурса для скачивания")
    parser.add_argument(
        "-n",
        "--requests",
        type=int,
        default=DEFAULT_REQUESTS,
        help=f"Количество последовательных запросов (по умолчанию: {DEFAULT_REQUESTS})",
    )
    parser.add_argument(
        "-t",
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"Тайм-аут одного запроса в секундах (по умолчанию: {DEFAULT_TIMEOUT})",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Вывести итоговый отчёт в формате JSON",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    def print_step(sample: RequestSample, total: int) -> None:
        step_mbps = (
            (sample.bytes_read * 8) / (sample.elapsed_s * 1_000_000)
            if sample.elapsed_s > 0
            else 0.0
        )
        print(
            f"[{sample.index}/{total}] {sample.bytes_read / 1_000_000:.2f} MB "
            f"за {sample.elapsed_s:.3f} с (TTFB {sample.ttfb_s * 1000:.0f} мс, {step_mbps:.2f} Mbps)"
        )

    try:
        result = run_benchmark(
            args.url,
            count=args.requests,
            timeout=args.timeout,
            on_progress=None if args.as_json else print_step,
        )
    except ValueError as err:
        print(f"Ошибка аргументов: {err}", file=sys.stderr)
        return 2
    except urllib.error.HTTPError as err:
        print(f"HTTP-ошибка {err.code}: {err.reason}", file=sys.stderr)
        return 1
    except DownloadError as err:
        print(f"Ошибка загрузки: {err}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        print(f"Сетевая ошибка: {err}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nЗамер прерван пользователем.", file=sys.stderr)
        return 130

    if args.as_json:
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        return 0

    print("-" * 52)
    print(f"Запросов выполнено: {result.requests_count}")
    print(f"Скачано всего:      {format_size(result.total_bytes)}")
    print(f"Общее время:        {result.total_time_s:.3f} с")
    print(
        f"Время запроса:      avg {result.avg_time_s:.3f} с | "
        f"med {result.median_time_s:.3f} с | "
        f"min {result.min_time_s:.3f} с | max {result.max_time_s:.3f} с"
    )
    print(f"Средний TTFB:       {result.avg_ttfb_s * 1000:.0f} мс")
    print(f"Скорость:           {result.speed_mbps:.2f} Mbps ({result.speed_mb_s:.2f} MB/s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())