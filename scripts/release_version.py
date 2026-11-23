"""PRのマージ時に、版のタグ（vMAJOR.MINOR.PATCH）とGitHub Releaseをどう作るかを決める。

再利用可能ワークフロー .github/workflows/release-tag.yml から `python3 <このファイル>` で呼ばれる。
標準ライブラリとgitだけで動く（GitHub Actionsのランナーの標準のpython3で実行するため、依存を足さない。Python 3.10以降）。

入力：カレントディレクトリのgitリポジトリ（全履歴とタグを取得済みであること）と、次の環境変数。
- MERGE_SHA：版を付ける対象のコミット（40桁のSHA）。
- RELEASE_LABELS：PRのラベル名のJSON配列。
- RELEASE_MAJOR_LABEL・RELEASE_MINOR_LABEL：桁を上げるラベル名（省略時は major・minor）。
- RELEASE_FIRST_VERSION：正式な版のタグがまだないときの最初の版（省略時は v1.0.0）。
出力：標準出力に `action=` `version=` `previous=` の3行（GITHUB_OUTPUTにそのまま追記できる形）。

action の意味：
- create：新しい版のタグとReleaseを作る。
- existing：対象のコミットにすでに正式な版のタグがある（再実行）。Releaseがなければ、そのタグで作る。
- contained：対象のコミットは、すでに打った最新の版に含まれている（実行の順序が逆転した）。何も作らない。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass

# 打つ対象の版。`v1.0.0-rc1`や`latest`のような、3つの数字だけでないタグは数えない。
TAG_PATTERN = re.compile(r'^(?:refs/tags/)?v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$')
# 環境変数を省略したときの既定値。最初の版は、正式な版のタグがまだ1つもないときだけ使う。
DEFAULT_FIRST_VERSION = 'v1.0.0'
DEFAULT_MAJOR_LABEL = 'major'
DEFAULT_MINOR_LABEL = 'minor'

Version = tuple[int, int, int]


class ReleaseError(Exception):
    """版を決められない状態。workflowを失敗させ、人が確かめる。"""


@dataclass(frozen=True)
class Rules:
    """呼び出し側のリポジトリが選べる、版の上げ方。"""

    major_label: str = DEFAULT_MAJOR_LABEL
    minor_label: str = DEFAULT_MINOR_LABEL
    first_version: str = DEFAULT_FIRST_VERSION


@dataclass(frozen=True)
class Plan:
    """workflowが行う操作。versionはタグ名、previousはリリースノートの起点（なければ空文字）。"""

    action: str
    version: str
    previous: str


def parse_version(tag: str) -> Version | None:
    """タグ名が正式な版の形なら数字の組を、そうでなければNoneを返す。"""
    match = TAG_PATTERN.match(tag.strip())
    if not match:
        return None
    return (int(match[1]), int(match[2]), int(match[3]))


def parse_versions(tags: list[str]) -> list[Version]:
    """タグ名の一覧から、版の形のものだけを数字の組にして返す。形が違うタグは無視する。"""
    return [version for version in (parse_version(tag) for tag in tags) if version is not None]


def format_version(version: Version) -> str:
    """数字の組を `vMAJOR.MINOR.PATCH` の文字列にする。"""
    return 'v{}.{}.{}'.format(*version)


def latest_version(tags: list[str]) -> str:
    """既存のタグのうち最新の版を返す。版の形のタグがなければ空文字を返す。数字で比べる（v1.10.0はv1.9.0より新しい）。"""
    versions = parse_versions(tags)
    return format_version(max(versions)) if versions else ''


def bump_level(labels: list[str], rules: Rules = Rules()) -> str:
    """PRのラベルから、上げる桁（major・minor・patch）を決める。ラベルがなければpatch、両方あればmajorを優先する。"""
    if rules.major_label in labels:
        return 'major'
    if rules.minor_label in labels:
        return 'minor'
    return 'patch'


def next_version(tags: list[str], labels: list[str], rules: Rules = Rules()) -> str:
    """既存のタグとPRのラベルから、次の版を返す。タグがまだなければ最初の版（既定はv1.0.0）にする。"""
    versions = parse_versions(tags)
    if not versions:
        return rules.first_version
    major, minor, patch = max(versions)
    level = bump_level(labels, rules)
    if level == 'major':
        # 上の桁を上げたら、下の桁は0に戻す。
        return format_version((major + 1, 0, 0))
    if level == 'minor':
        return format_version((major, minor + 1, 0))
    return format_version((major, minor, patch + 1))


def plan_release(
    tags: dict[str, str],
    target: str,
    labels: list[str],
    is_ancestor: Callable[[str, str], bool],
    rules: Rules = Rules(),
) -> Plan:
    """タグ名→指すコミットの対応と対象のコミットから、行う操作を決める。

    tags の値は、注釈付きタグでもタグオブジェクトではなく、指すコミットのSHAであること。
    is_ancestor(a, b) は、コミットaがbの祖先（または同じ）ならTrueを返す関数。
    版を決められない（履歴が分岐している、順序の逆転でラベルの桁上げが失われる）ときは ReleaseError を送出する。
    """
    versions = {name: version for name, version in ((name, parse_version(name)) for name in tags) if version is not None}
    on_target = [version for name, version in versions.items() if tags[name] == target]
    if on_target:
        # 再実行。対象のコミットの版を使い、Releaseがなければworkflowがそのタグで作る。
        version = max(on_target)
        older = [other for other in versions.values() if other < version]
        return Plan('existing', format_version(version), format_version(max(older)) if older else '')
    if not versions:
        return Plan('create', rules.first_version, '')
    latest = max(versions.values())
    latest_name = format_version(latest)
    latest_commit = next(tags[name] for name, version in versions.items() if version == latest)
    if is_ancestor(latest_commit, target):
        return Plan('create', next_version(list(versions), labels, rules), latest_name)
    if is_ancestor(target, latest_commit):
        # 後にマージしたPRの実行が先に版を打った。この変更はその版に含まれるので、版を作らない。
        # ただし、minor・majorの桁上げは、どの版にも反映されないまま失われるので、失敗にして人に知らせる。
        level = bump_level(labels, rules)
        if level != 'patch':
            raise ReleaseError(
                f'このコミットはすでに{latest_name}に含まれていますが、{level}ラベルの桁上げは反映されていません。'
                '必要なら、手動で版を作ってください。'
            )
        return Plan('contained', latest_name, '')
    # mainの書き換えなどで、最新の版と対象のコミットの履歴が分かれている。自動では決めない。
    raise ReleaseError(f'最新の版{latest_name}（{latest_commit}）と、対象のコミット（{target}）の履歴が分かれています。')


def run_git(*args: str) -> subprocess.CompletedProcess[str]:
    """gitを実行し、結果を返す。終了コードは呼び出し側で判断する。"""
    return subprocess.run(['git', *args], capture_output=True, text=True, check=False)


def read_tags_from_git() -> dict[str, str]:
    """正式な版の形のタグ名→指すコミットのSHAを、gitから読む。注釈付きタグは、指すコミットまでたどる。"""
    listed = run_git('for-each-ref', '--format=%(refname:short)', 'refs/tags/v*')
    if listed.returncode != 0:
        raise ReleaseError(f'タグの一覧を読めません: {listed.stderr.strip()}')
    names = [name for name in listed.stdout.splitlines() if parse_version(name) is not None]
    if not names:
        return {}
    # `^{commit}` で、軽量タグも注釈付きタグも、指すコミットのSHAにそろえる。
    resolved = run_git('rev-parse', *(f'refs/tags/{name}^{{commit}}' for name in names))
    if resolved.returncode != 0:
        raise ReleaseError(f'タグの指すコミットを読めません: {resolved.stderr.strip()}')
    return dict(zip(names, resolved.stdout.split(), strict=True))


def git_is_ancestor(ancestor: str, descendant: str) -> bool:
    """コミットancestorがdescendantの祖先（または同じ）ならTrueを返す。gitが判定できなければ ReleaseError を送出する。"""
    result = run_git('merge-base', '--is-ancestor', ancestor, descendant)
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    # 終了コード1以外は、コミットが取得されていないなどの異常。祖先でないとみなして版を作らない。
    raise ReleaseError(f'コミットの前後関係を判定できません: {result.stderr.strip()}')


def read_labels(raw: str) -> list[str]:
    """RELEASE_LABELSのJSON配列を文字列のリストにする。空なら空のリスト。配列でない・文字列以外が混ざるときは例外を送出する。"""
    if not raw.strip():
        return []
    labels = json.loads(raw)
    if not isinstance(labels, list) or not all(isinstance(label, str) for label in labels):
        raise ValueError('RELEASE_LABELS must be a JSON array of strings')
    return labels


def read_rules(environ: Mapping[str, str]) -> Rules:
    """環境変数から版の上げ方を読む。空の値は既定値にする。不正な値は ValueError を送出する（誤った版を打たない）。"""
    rules = Rules(
        major_label=environ.get('RELEASE_MAJOR_LABEL', '').strip() or DEFAULT_MAJOR_LABEL,
        minor_label=environ.get('RELEASE_MINOR_LABEL', '').strip() or DEFAULT_MINOR_LABEL,
        first_version=environ.get('RELEASE_FIRST_VERSION', '').strip() or DEFAULT_FIRST_VERSION,
    )
    if rules.major_label == rules.minor_label:
        raise ValueError('RELEASE_MAJOR_LABEL and RELEASE_MINOR_LABEL must differ')
    if parse_version(rules.first_version) is None or rules.first_version.startswith('refs/'):
        raise ValueError('RELEASE_FIRST_VERSION must look like vMAJOR.MINOR.PATCH')
    return rules


def main() -> int:
    """gitのタグとMERGE_SHA・RELEASE_LABELSから、行う操作を標準出力に書く。決められなければ、標準エラーに理由を出して1を返す。"""
    try:
        labels = read_labels(os.environ.get('RELEASE_LABELS', ''))
    except ValueError as error:
        # JSONの構文エラー（ValueErrorの一種）も、ここでまとめて拒否する。誤った桁で版を上げない。
        print(f'RELEASE_LABELSが不正です: {error}', file=sys.stderr)
        return 1
    try:
        rules = read_rules(os.environ)
    except ValueError as error:
        print(f'版の上げ方の指定が不正です: {error}', file=sys.stderr)
        return 1
    target = os.environ.get('MERGE_SHA', '').strip()
    if not re.fullmatch(r'[0-9a-f]{40}', target):
        print('MERGE_SHAに、40桁のコミットSHAを指定してください。', file=sys.stderr)
        return 1
    try:
        plan = plan_release(read_tags_from_git(), target, labels, git_is_ancestor, rules)
    except ReleaseError as error:
        print(f'::error::{error}', file=sys.stderr)
        return 1
    if plan.action == 'contained':
        print(f'::notice::このコミットは、すでに{plan.version}に含まれています。版は作りません。', file=sys.stderr)
    print(f'action={plan.action}')
    print(f'version={plan.version}')
    print(f'previous={plan.previous}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
