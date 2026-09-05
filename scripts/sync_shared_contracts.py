"""Vendor pure contracts into independent Tool packages; never import Agent there."""
from pathlib import Path
import argparse

FILES = ('browser_lock_contract.py','discovery_contract.py')


def sync(source,tools_root,*,check=False):
    target=tools_root/'social_content_crawler/src/social_content_crawler'
    if not target.is_dir() or not (target.parents[1]/'pyproject.toml').is_file():
        raise ValueError('Not a social Tool source directory')
    contents={name:(source/name).read_bytes() for name in FILES}
    changed=[]
    for name,content in contents.items():
        path=target/name
        if not path.is_file() or path.read_bytes()!=content:
            changed.append(path)
            if not check: path.write_bytes(content)
    return changed


def main():
    project=Path(__file__).resolve().parents[1]
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tools-root',type=Path,default=project.parent/'tools')
    parser.add_argument('--check',action='store_true')
    args=parser.parse_args()
    changed=sync(project/'src/social_ops_agent',args.tools_root,check=args.check)
    for path in changed: print(('Stale: ' if args.check else 'Updated: ')+str(path))
    return int(bool(changed) and args.check)


if __name__=='__main__': raise SystemExit(main())
