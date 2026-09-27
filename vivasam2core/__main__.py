"""Command line: python -m vivasam2core -t TEMPLATE.hwp -o OUTDIR VIVASAM.hwp [...]"""
import argparse
import os
import sys

from .convert import convert, parse_source


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog='python -m vivasam2core',
        description='비바샘 문제은행 HWP(HWPML)를 CORE 교재 서식(템플릿 HWP)으로 변환합니다.')
    ap.add_argument('sources', nargs='+', help='비바샘에서 내려받은 문제 파일(.hwp/.hml)')
    ap.add_argument('-t', '--template', required=True,
                    help='서식 원본 HWP (예: "2-1 교정적 정의(심화)(답지).hwp")')
    ap.add_argument('-o', '--outdir', default='output', help='결과 저장 폴더 (기본: output)')
    ap.add_argument('--title', help='머리말 "실전 문제" 뒤에 들어갈 제목 (기본: 과목명)')
    ap.add_argument('--student', action='store_true',
                    help='정답 표시 없는 문제지 버전도 함께 만듭니다')
    args = ap.parse_args(argv)

    os.makedirs(args.outdir, exist_ok=True)
    for src in args.sources:
        doc, _ = parse_source(src)
        name = doc.title or os.path.splitext(os.path.basename(src))[0]
        base = name if src.lower().endswith('.docx') else '비바샘 %s' % name
        jobs = [(True, base + '(심화)(답지).hwp')]
        if args.student:
            jobs.append((False, base + '(심화).hwp'))
        for answer_mode, name in jobs:
            out = os.path.join(args.outdir, name)
            info = convert(args.template, src, out, answer_mode=answer_mode, title=args.title)
            print('%s -> %s  (%d문항, 그림 %d개, %d bytes)' % (
                os.path.basename(src), out, info['problems'], info['images'], info['size']))
            for w in info['warnings']:
                print('  주의: ' + w)
    return 0


if __name__ == '__main__':
    sys.exit(main())
