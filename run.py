#!/usr/bin/env python3
"""Run Corpus as one process; no asset build is needed."""
import argparse, os

def main():
    parser=argparse.ArgumentParser(description='Corpus literature review manager')
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=8000)
    parser.add_argument('--dev',action='store_true',help='Disable login for loopback-only development')
    args=parser.parse_args()
    if args.dev:
        if args.host not in ('127.0.0.1','::1','localhost'): parser.error('--dev is restricted to a loopback bind')
        os.environ['CORPUS_DEV']='1'
    if os.getenv('CORPUS_DEV')=='1' and args.host not in ('127.0.0.1','::1','localhost'): parser.error('CORPUS_DEV requires a loopback bind')
    import uvicorn
    from corpus.app import create_app
    uvicorn.run(create_app(),host=args.host,port=args.port,proxy_headers=True,forwarded_allow_ips='127.0.0.1,::1')

if __name__=='__main__': main()
