"""CLI boundary: supplied parser; repairs live in student_tasks.py."""
import argparse
import json
import logging
import sys
import student_tasks as tasks
import workflow

def parser():
    root = argparse.ArgumentParser(description='Discover Sentinel scenes on demand; one JPEG preview at most.')
    commands = root.add_subparsers(dest='command',required=True)
    for name in ('discover','report'):
        command = commands.add_parser(name)
        command.add_argument('--config',required=True,help='Path to a JSON configuration file')
        if name=='discover':
            command.add_argument('--until',help='Optional fixed end with timezone, e.g. 2025-06-15T00:00:00Z')
            command.add_argument('--preview',action='store_true',help='Download/cache one newest available JPEG preview')
            command.add_argument('--lookback-days',type=int,help='Override overlap for this run; cannot go before initial_start')
    return root

def main(argv=None):
    args = parser().parse_args(argv)
    logger = logging.getLogger('sentinel')
    def job():
        config = workflow.load_config(args.config)
        paths = tasks.resolve_paths(config,args.config)
        workflow.configure_logging(paths['log_file'])
        if args.command=='report':
            return workflow.report(config,paths)
        until = tasks.choose_until(args.until)
        return workflow.run_discovery(config,paths,until,preview=args.preview,lookback_days=args.lookback_days)
    code,result = tasks.execute_job(job,logger)
    print(json.dumps(result,indent=2))
    return code

if __name__=='__main__':
    sys.exit(main())
