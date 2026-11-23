"""PRのマージ時に、版のタグとReleaseをどう作るかを決める処理（scripts/release_version.py）の試験。"""
import subprocess
from pathlib import Path

import pytest

from scripts import release_version
from scripts.release_version import Plan, ReleaseError, Rules, bump_level, latest_version, next_version, plan_release, read_labels, read_rules

# 直線の履歴 c1 → c2 → c3 を表す。plan_release の試験で、gitの代わりに前後関係を返す。
LINEAR_HISTORY = ['c1', 'c2', 'c3']


def linear_is_ancestor(ancestor: str, descendant: str) -> bool:
    """LINEAR_HISTORY上で、ancestorがdescendantより前（または同じ）ならTrueを返す。履歴にないコミットは分岐とみなす。"""
    if ancestor not in LINEAR_HISTORY or descendant not in LINEAR_HISTORY:
        return False
    return LINEAR_HISTORY.index(ancestor) <= LINEAR_HISTORY.index(descendant)


def test_first_version_when_no_tags_exist() -> None:
    """タグが1つもなければ、ラベルにかかわらず最初の版（v1.0.0）にする。"""
    assert next_version([], []) == 'v1.0.0'
    assert next_version([], ['major']) == 'v1.0.0'


def test_patch_is_the_default_without_labels() -> None:
    """ラベルがなければ、最後の桁だけを上げる。"""
    assert next_version(['v1.0.0'], []) == 'v1.0.1'
    assert next_version(['v1.0.0'], ['docs', 'enhancement']) == 'v1.0.1'


def test_minor_label_resets_patch() -> None:
    """minorラベルは、2番目の桁を上げて、最後の桁を0に戻す。"""
    assert next_version(['v1.2.7'], ['minor']) == 'v1.3.0'


def test_major_label_resets_minor_and_patch() -> None:
    """majorラベルは、最初の桁を上げて、残りを0に戻す。"""
    assert next_version(['v1.2.7'], ['major']) == 'v2.0.0'


def test_major_wins_when_both_labels_are_set() -> None:
    """minorとmajorの両方が付いたときは、大きいほうを使う。"""
    assert bump_level(['minor', 'major']) == 'major'
    assert next_version(['v1.2.7'], ['minor', 'major']) == 'v2.0.0'


def test_versions_are_compared_as_numbers_not_text() -> None:
    """v1.10.0はv1.9.0より新しい。文字列の並びで比べない。"""
    tags = ['v1.9.0', 'v1.10.0', 'v1.2.0']
    assert latest_version(tags) == 'v1.10.0'
    assert next_version(tags, []) == 'v1.10.1'


def test_non_version_tags_are_ignored() -> None:
    """3つの数字でない版（先行版・2桁・latestなど）のタグは、数えない。"""
    tags = ['v1.0.0', 'v2.0.0-rc1', 'v3.0', 'latest', 'v01.0.0', 'release-9.9.9', 'v1.0.0+build']
    assert latest_version(tags) == 'v1.0.0'
    assert next_version(tags, []) == 'v1.0.1'


def test_only_non_version_tags_start_from_the_first_version() -> None:
    """版の形のタグがなければ、最初の版から始める。いまの最新は空になる。"""
    assert latest_version(['latest', 'v2.0.0-rc1']) == ''
    assert next_version(['latest', 'v2.0.0-rc1'], []) == 'v1.0.0'


def test_ref_names_from_the_api_are_accepted() -> None:
    """GitHubのAPIが返す`refs/tags/`つきの名前も、そのまま扱える。"""
    assert next_version(['refs/tags/v1.0.0', 'refs/tags/v1.0.3\n'], []) == 'v1.0.4'


def test_read_labels_accepts_empty_and_arrays() -> None:
    """ラベルの指定が空なら空のリスト、JSON配列ならその中身を返す。"""
    assert read_labels('') == []
    assert read_labels('  ') == []
    assert read_labels('["minor", "docs"]') == ['minor', 'docs']


@pytest.mark.parametrize('raw', ['{"minor": true}', '["minor", 1]', '"minor"', 'minor', '[,'])
def test_read_labels_rejects_invalid_input(raw: str) -> None:
    """配列でない・文字列以外が混ざる・JSONとして壊れている入力は、例外にする（誤った桁で版を上げないため）。"""
    with pytest.raises(ValueError):
        read_labels(raw)


def test_plan_creates_first_version_without_tags() -> None:
    """正式な版のタグがなければ、最初の版を作る。"""
    assert plan_release({}, 'c1', [], linear_is_ancestor) == Plan('create', 'v1.0.0', '')


def test_plan_creates_next_version_after_latest() -> None:
    """最新の版が対象の祖先なら、次の版を作り、最新の版をリリースノートの起点にする。"""
    tags = {'v1.0.0': 'c1', 'v1.0.1': 'c2'}
    assert plan_release(tags, 'c3', ['minor'], linear_is_ancestor) == Plan('create', 'v1.1.0', 'v1.0.1')


def test_plan_reuses_existing_formal_tag_on_target() -> None:
    """対象にすでに正式な版のタグがあれば、新しい版を作らず、そのタグを使う（Releaseがなければworkflowが作る）。"""
    tags = {'v1.0.0': 'c1', 'v1.0.1': 'c2'}
    assert plan_release(tags, 'c2', [], linear_is_ancestor) == Plan('existing', 'v1.0.1', 'v1.0.0')


def test_plan_ignores_prerelease_tag_on_target() -> None:
    """対象に先行版のタグ（v1.1.0-rc1）しかなければ、リリース済みとみなさず、次の版を作る。"""
    tags = {'v1.0.0': 'c1', 'v1.1.0-rc1': 'c2'}
    assert plan_release(tags, 'c2', [], linear_is_ancestor) == Plan('create', 'v1.0.1', 'v1.0.0')


def test_plan_skips_target_already_contained_in_newer_version() -> None:
    """後にマージしたPRが先に版を打った（順序の逆転）なら、古い対象に新しい版を作らない。"""
    tags = {'v1.0.0': 'c1', 'v1.0.1': 'c3'}
    assert plan_release(tags, 'c2', [], linear_is_ancestor) == Plan('contained', 'v1.0.1', '')


@pytest.mark.parametrize('label', ['minor', 'major'])
def test_plan_fails_when_reordering_loses_a_bump(label: str) -> None:
    """順序の逆転で、minor・majorの桁上げがどの版にも反映されないなら、失敗にして人に知らせる。"""
    tags = {'v1.0.0': 'c1', 'v1.0.1': 'c3'}
    with pytest.raises(ReleaseError, match=label):
        plan_release(tags, 'c2', [label], linear_is_ancestor)


def test_plan_fails_when_history_diverged() -> None:
    """最新の版と対象の履歴が分かれていれば、自動で版を決めない。"""
    with pytest.raises(ReleaseError):
        plan_release({'v1.0.0': 'c1'}, 'other', [], linear_is_ancestor)


def git(repo: Path, *args: str) -> str:
    """試験用のリポジトリでgitを実行し、標準出力を返す。"""
    result = subprocess.run(
        ['git', '-c', 'user.name=test', '-c', 'user.email=test@example.invalid', '-c', 'tag.gpgSign=false', '-c', 'commit.gpgSign=false', *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def commit(repo: Path, message: str) -> str:
    """空のコミットを作り、そのSHAを返す。"""
    git(repo, 'commit', '--allow-empty', '-q', '-m', message)
    return git(repo, 'rev-parse', 'HEAD')


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """空の試験用gitリポジトリを作り、カレントディレクトリにする。"""
    git(tmp_path, 'init', '-q')
    monkeypatch.chdir(tmp_path)
    for name in ('RELEASE_LABELS', 'RELEASE_MAJOR_LABEL', 'RELEASE_MINOR_LABEL', 'RELEASE_FIRST_VERSION'):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def run_main(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], target: str, labels: str = '') -> tuple[int, str, str]:
    """MERGE_SHAとRELEASE_LABELSを設定してmainを実行し、終了コード・標準出力・標準エラーを返す。"""
    monkeypatch.setenv('MERGE_SHA', target)
    monkeypatch.setenv('RELEASE_LABELS', labels)
    code = release_version.main()
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_main_creates_next_version_from_git(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """gitのタグから最新の版を読み、GITHUB_OUTPUTの形で次の版を書く。"""
    first = commit(repo, 'first')
    git(repo, 'tag', 'v1.0.0', first)
    target = commit(repo, 'second')
    assert run_main(monkeypatch, capsys, target, '["minor"]') == (0, 'action=create\nversion=v1.1.0\nprevious=v1.0.0\n', '')


def test_main_resolves_annotated_tag_to_its_commit(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """注釈付きの正式タグは、タグオブジェクトではなく指すコミットで照合する（同じコミットに別の版を作らない）。"""
    first = commit(repo, 'first')
    git(repo, 'tag', 'v1.0.0', first)
    target = commit(repo, 'second')
    git(repo, 'tag', '-a', 'v1.0.1', '-m', 'release', target)
    assert run_main(monkeypatch, capsys, target) == (0, 'action=existing\nversion=v1.0.1\nprevious=v1.0.0\n', '')


def test_main_skips_older_merge_contained_in_newer_version(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """古いマージの実行を後から再試行しても、新しい版を古いコミットに作らない。"""
    first = commit(repo, 'first')
    git(repo, 'tag', 'v1.0.0', first)
    older = commit(repo, 'A')
    newer = commit(repo, 'B')
    git(repo, 'tag', 'v1.0.1', newer)
    code, out, err = run_main(monkeypatch, capsys, older)
    assert (code, out) == (0, 'action=contained\nversion=v1.0.1\nprevious=\n')
    assert '::notice::' in err


def test_main_fails_on_unknown_commit(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """取得されていないコミットでは、前後関係を判定できないので、版を出さずに1を返す。"""
    git(repo, 'tag', 'v1.0.0', commit(repo, 'first'))
    code, out, err = run_main(monkeypatch, capsys, 'f' * 40)
    assert (code, out) == (1, '')
    assert '::error::' in err


@pytest.mark.parametrize('target', ['', 'HEAD', 'abc123', 'A' * 40])
def test_main_rejects_invalid_merge_sha(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], target: str) -> None:
    """MERGE_SHAが40桁の小文字のSHAでなければ、gitを呼ばずに1を返す。"""
    code, out, err = run_main(monkeypatch, capsys, target)
    assert (code, out) == (1, '')
    assert 'MERGE_SHA' in err


def test_main_fails_closed_on_invalid_labels(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """ラベルの指定が不正なら、版を出さずに1を返す。"""
    code, out, err = run_main(monkeypatch, capsys, commit(repo, 'first'), 'not json')
    assert (code, out) == (1, '')
    assert 'RELEASE_LABELS' in err


def test_custom_labels_and_first_version() -> None:
    """呼び出し側が選んだラベル名と最初の版で、版を決める。既定のラベル名は桁を上げない。"""
    rules = Rules(major_label='breaking', minor_label='feature', first_version='v0.1.0')
    assert next_version([], [], rules) == 'v0.1.0'
    assert next_version(['v0.1.0'], ['feature'], rules) == 'v0.2.0'
    assert next_version(['v0.1.0'], ['breaking'], rules) == 'v1.0.0'
    assert next_version(['v0.1.0'], ['major', 'minor'], rules) == 'v0.1.1'


def test_read_rules_uses_defaults_for_empty_values() -> None:
    """環境変数が空か未設定なら、既定値（major・minor・v1.0.0）にする。"""
    assert read_rules({}) == Rules()
    assert read_rules({'RELEASE_MAJOR_LABEL': ' ', 'RELEASE_FIRST_VERSION': ''}) == Rules()


@pytest.mark.parametrize(
    'environ',
    [
        {'RELEASE_MAJOR_LABEL': 'same', 'RELEASE_MINOR_LABEL': 'same'},
        {'RELEASE_FIRST_VERSION': '1.0.0'},
        {'RELEASE_FIRST_VERSION': 'v1.0.0-rc1'},
        {'RELEASE_FIRST_VERSION': 'refs/tags/v1.0.0'},
    ],
)
def test_read_rules_rejects_invalid_values(environ: dict[str, str]) -> None:
    """同じラベル名を2つの桁に使う、最初の版が正式な版の形でない指定は、例外にする。"""
    with pytest.raises(ValueError):
        read_rules(environ)


def test_main_fails_closed_on_invalid_rules(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """版の上げ方の指定が不正なら、版を出さずに1を返す。"""
    monkeypatch.setenv('RELEASE_FIRST_VERSION', 'latest')
    code, out, err = run_main(monkeypatch, capsys, commit(repo, 'first'))
    assert (code, out) == (1, '')
    assert '版の上げ方' in err
