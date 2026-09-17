"""isolation — 在獨立子行程中執行「處理不可信內容」的函式,以行程邊界兜底資源耗盡。

用途:dbt 樣板展開(dbt_render)與 macro 反查(dbt_impact)都要處理待審 MR 的內容。
Jinja 沙箱與各模組內的上限擋得住常見手法,但擋不住「一個跑不完的迴圈」或
「一個吃光記憶體的字串」。這裡用行程邊界處理:

  * 超過 timeout_s 秒強制終止子行程
  * 支援的平台(Linux)以 RLIMIT_AS 限制子行程記憶體;Windows 上只有逾時
  * 子行程的任何異常(含 MemoryError、啟動失敗、被系統終止)一律收斂成失敗結果,
    訊息不含例外內容(避免帶出路徑或輸入內容)

兩個模組共用這一份實作,避免各寫一套、各有漏洞。

限制:
  * fn 必須是模組層級的函式,參數與回傳值必須可 pickle(multiprocessing spawn 的要求)
  * 呼叫端程式的進入點需有 `if __name__ == "__main__":` 保護
"""
import multiprocessing


def _worker(conn, fn, max_memory_mb: int, args: tuple, kwargs: dict) -> None:
    """子行程進入點:先設記憶體上限(支援的平台),再執行,結果經 pipe 傳回。"""
    try:
        import resource   # 僅 Unix 有

        limit = max_memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except (ImportError, ValueError, OSError):
        pass              # Windows 等平台:只剩逾時與各模組內的上限
    try:
        message = ("ok", fn(*args, **kwargs))
    except MemoryError:
        message = ("error", "MemoryError: 執行時記憶體不足(超過上限),已中止")
    except BaseException as e:   # 不帶例外內容,只留類型
        message = ("error", f"{type(e).__name__}: 子行程執行失敗")
    try:
        conn.send(message)
    except BaseException:
        pass
    finally:
        conn.close()


def run_isolated(fn, args: tuple = (), kwargs: dict | None = None, *,
                 timeout_s: float, max_memory_mb: int, on_failure):
    """在子行程中執行 fn(*args, **kwargs)。

    成功時回傳 fn 的結果;任何失敗(逾時、異常、資源耗盡、回傳格式不對)時回傳
    on_failure(錯誤訊息)。on_failure 只在本行程執行,不需要可 pickle。
    """
    ctx = multiprocessing.get_context("spawn")
    recv_conn, send_conn = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_worker, daemon=True,
                       args=(send_conn, fn, max_memory_mb, tuple(args), dict(kwargs or {})))
    proc.start()
    send_conn.close()
    message = None
    timed_out = False
    try:
        try:
            if recv_conn.poll(timeout_s):
                message = recv_conn.recv()
            else:
                timed_out = True
        except (EOFError, OSError):
            message = None       # 子行程沒送出結果就結束(例如被系統因記憶體不足終止)
    finally:
        recv_conn.close()
        if proc.is_alive():
            proc.terminate()
            proc.join(5)
            if proc.is_alive():
                proc.kill()
        proc.join(5)

    if timed_out:
        return on_failure(f"IsolationError: 執行逾時(超過 {timeout_s} 秒),已強制終止")
    if message is None:
        return on_failure(
            f"IsolationError: 子行程異常結束(結束代碼 {proc.exitcode}),"
            f"可能是資源耗盡或執行環境無法啟動子行程")
    if not (isinstance(message, tuple) and len(message) == 2 and message[0] in ("ok", "error")):
        return on_failure("IsolationError: 子行程回傳了非預期的資料")
    status, payload = message
    if status == "error":
        return on_failure(str(payload))
    return payload
