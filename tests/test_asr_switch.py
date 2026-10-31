#coding=utf-8
"""
换引擎时的显存账（不需要 GPU、不需要模型，用 mock 引擎 + 假显存）

踩过的坑：POST /engine 切 qwen3-asr-0.6b 报
    显存不足：GTX 1080 Ti 空闲 1312MB < 引擎 qwen3-asr-0.6b 需要 2600MB
因为显存守卫跑的时候，旧引擎链（流式 840 + ct-punc 1.1G + 精修 840 ≈ 2.8GB）还占着卡。
「换」被当成了「加」。这里把这条账钉死：

  1. 守卫必须看到「旧引擎已经让位」之后的空闲
  2. 新引擎装不上时要能回滚，别把服务拖成裸奔
  3. 精修引擎装不上只降级，不整个失败
  4. 报错信息里带上当时的显存快照

跑法：conda run -n moneyprinter python -m pytest tests/test_asr_switch.py -v
"""

import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASR_DIR = os.path.join(REPO, "accessories", "ASR")
if ASR_DIR not in sys.path:
    sys.path.insert(0, ASR_DIR)

import asr_engines as E                    # noqa: E402
import ASR_server as S                     # noqa: E402

TOTAL = 11165.0
RESIDENT_FREE = 1300.0      # 旧引擎链还占着卡时，驱动看到的空闲
FREED_FREE = 4100.0         # 旧引擎卸载 + empty_cache 之后


def make_app(engine="mock", refine="", device="cuda"):
    cfg = S.Config(engine=engine, device=device, punc=False, warmup=False,
                   refine=refine, spk="")
    app = S.AsrApp(cfg)
    app.device = "cuda:0"                   # 假装在 GPU 上，守卫才会生效
    return app


def build_on_gpu(real_build, fail_on=()):
    """mock 引擎内部强制 device=cpu（守卫会直接跳过），这里把实例改回 cuda。"""
    def build(name, **kw):
        if name in fail_on:
            raise E.EngineUnavailable(f"显存不足（模拟 {name}）")
        eng = real_build(name, **kw)
        eng.device = "cuda:0"
        return eng
    return build


def fake_free_mb(app, monkeypatch):
    """空闲显存 = 卡上有没有驻留模型，直接挂钩 app.engine。"""
    def gpu_free_mb(device):
        return (RESIDENT_FREE if app.engine is not None else FREED_FREE), TOTAL, "fake-gpu"
    monkeypatch.setattr(E, "gpu_free_mb", gpu_free_mb)


def mock_load_checks_vram(monkeypatch):
    """真实引擎（funasr/whisper/qwen）load() 第一件事就是 guard_vram，
    mock 引擎显存要求是 0 所以本来就跳过；这里补上，让守卫进入被测路径。"""
    real_load = E.MockEngine.load

    def load(self):
        self.guard_vram(force=getattr(self, "force_vram", False))
        return real_load(self)
    monkeypatch.setattr(E.MockEngine, "load", load)


def test_switch_unloads_the_old_engine_before_the_vram_guard(monkeypatch):
    app = make_app()
    app.load_engine("mock")
    old = app.engine
    assert old.is_loaded()

    monkeypatch.setattr(E.ENGINE_SPECS["mock-offline"], "min_vram_mb", 2600)
    fake_free_mb(app, monkeypatch)
    monkeypatch.setattr(E, "build_engine", build_on_gpu(E.build_engine))
    mock_load_checks_vram(monkeypatch)

    seen = []
    real_guard = E.BaseEngine.guard_vram

    def spy(self, force=False):
        seen.append(old.is_loaded())        # 守卫跑的时候，旧引擎还在不在卡上
        return real_guard(self, force)
    monkeypatch.setattr(E.BaseEngine, "guard_vram", spy)

    info = app.switch_engine("mock-offline")
    assert info["name"] == "mock-offline"
    assert seen, "守卫没跑"
    assert seen[0] is False, "显存守卫跑的时候，旧引擎必须已经让位"
    assert app.engine is not old and app.engine.is_loaded()


def test_reloading_the_same_engine_also_unloads_it_first(monkeypatch):
    """POST /engine 传「当前引擎名」= 重载（精修没装上时重试就靠它）：同样必须先让位。

    同名重载以前走的是「不是切换」那条分支，于是新实例在旧实例还在卡上时就跑守卫，
    等于把重载当成再加一份 —— 精修重试时必然报显存不足。
    """
    app = make_app()
    app.load_engine("mock")
    old = app.engine

    monkeypatch.setattr(E.ENGINE_SPECS["mock"], "min_vram_mb", 2600)
    fake_free_mb(app, monkeypatch)
    monkeypatch.setattr(E, "build_engine", build_on_gpu(E.build_engine))
    mock_load_checks_vram(monkeypatch)

    seen = []
    real_guard = E.BaseEngine.guard_vram

    def spy(self, force=False):
        seen.append(old.is_loaded())
        return real_guard(self, force)
    monkeypatch.setattr(E.BaseEngine, "guard_vram", spy)

    info = app.switch_engine("mock")            # 同名 = 重载
    assert info["name"] == "mock"
    assert seen and seen[0] is False, "同名重载也必须先卸载旧实例再跑显存守卫"
    assert app.engine is not old and app.engine.is_loaded()


def test_switch_would_still_fail_if_the_new_engine_really_does_not_fit(monkeypatch):
    """让位之后仍然不够 → 该失败还是要失败（不能为了切换把守卫废掉）。"""
    app = make_app()
    app.load_engine("mock")
    monkeypatch.setattr(E.ENGINE_SPECS["mock-offline"], "min_vram_mb", 9000)
    fake_free_mb(app, monkeypatch)
    monkeypatch.setattr(E, "build_engine", build_on_gpu(E.build_engine))
    mock_load_checks_vram(monkeypatch)

    with pytest.raises(E.EngineUnavailable) as ei:
        app.switch_engine("mock-offline")
    assert "9000" in str(ei.value)
    assert "fake-gpu" in str(ei.value) and "此刻显存" in str(ei.value)


def test_failed_switch_puts_the_old_engine_back(monkeypatch):
    app = make_app()
    app.load_engine("mock")
    old = app.engine

    monkeypatch.setattr(E.ENGINE_SPECS["mock-offline"], "min_vram_mb", 2600)
    fake_free_mb(app, monkeypatch)
    monkeypatch.setattr(E, "build_engine", build_on_gpu(E.build_engine, fail_on=("mock-offline",)))

    with pytest.raises(E.EngineUnavailable):
        app.switch_engine("mock-offline")
    assert app.engine is old, "新引擎装不上，得把原来的装回去"
    assert old.is_loaded()
    assert app.state == "ready"


def test_refiner_failure_degrades_instead_of_killing_the_switch(monkeypatch):
    """主引擎已经起来了，精修装不上就降级跑，别把整次切换判失败。"""
    app = make_app(refine="mock-offline")
    app.load_engine("mock")

    real = E.build_engine

    def flaky(name, **kw):
        eng = build_on_gpu(real)(name, **kw)
        if name == "mock-offline":
            def boom():
                raise E.EngineUnavailable("精修显存不足（模拟）")
            eng.load = boom
        return eng
    monkeypatch.setattr(E, "build_engine", flaky)

    info = app.switch_engine("mock")
    assert info["name"] == "mock"
    assert app.engine is not None and app.engine.is_loaded()
    assert app.refiner is None
    assert "refine" in app.last_error


def test_switch_is_refused_while_sessions_are_active():
    app = make_app()
    app.load_engine("mock")

    class FakeSession:
        pass
    app.sessions.append(FakeSession())
    with pytest.raises(E.EngineError):
        app.switch_engine("mock-offline")
    assert app.engine.is_loaded(),  "被拒绝的切换不能已经把旧引擎卸了"


# -- 标点模型：自带标点的引擎自动跳过 ---------------------------------------

def test_self_punctuating_engines_auto_skip_ct_punc():
    """Qwen3-ASR 输出自带标点：自动模式下不加载 ct-punc（省 ~1.1GB 显存）。

    以前这条规则藏在 `punc and spec.family in ("funasr","mock")` 里，而 punc_enabled
    记的是传进来的原始值 —— /health 于是对 qwen 报 punc:true，模型其实没加载。
    """
    for name in ("qwen3-asr-0.6b", "qwen3-asr-1.7b"):
        eng = S.E.build_engine(name, punc=None)
        assert eng.punc_enabled is False, name
        assert eng.punc.enabled is False, name     # PuncModel 也不会被启用
        assert eng.punc.loaded is False, name


def test_auto_rule_keeps_punc_for_engines_that_need_it():
    for name in ("paraformer-streaming", "paraformer", "sensevoice", "mock", "mock-offline"):
        eng = S.E.build_engine(name, device="cpu", punc=None)
        assert eng.punc_enabled is True, name
    # ct-punc 是 FunASR 的模型，whisper 挂不上它
    assert S.E.build_engine("whisper", device="cpu", punc=None).punc_enabled is False


def test_explicit_punc_flags_beat_the_auto_rule():
    assert S.E.build_engine("qwen3-asr-0.6b", punc=True).punc_enabled is True            # --punc
    eng = S.E.build_engine("paraformer-streaming", device="cpu", punc=False)
    assert eng.punc_enabled is False                                                     # --no-punc
    assert eng.punc.loaded is False


def test_switching_between_engines_redecides_punc():
    """标点跟着引擎走：换到自带标点的引擎就不该再占 ct-punc，换回来要重新加上。"""
    app = make_app()
    app.load_engine("mock")
    assert app.engine.punc_enabled is False          # 这个 fixture 显式 --no-punc
    app.cfg.punc = None                              # 改回自动档
    app.load_engine("mock-offline")
    assert app.engine.punc_enabled is True           # mock 系不带标点 → 自动加
    app.cfg.engine = "mock"
    app.load_engine("mock")
    assert app.engine.punc_enabled is True


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
