cd /home/user/fastapi_e-commerce_backend
O=.cloud_torture/scratch_3a
PG=postgresql+psycopg://torture:torture@localhost:5432/postgres
F="tests/torture_suite/test_protocol_rfc_torture.py tests/torture_suite/test_boundary_mutants.py"
run(){ name=$1; shift; ( s=$(date +%s); env "$@" > $O/final_$name.txt 2>&1; echo "wall $(( $(date +%s)-s ))s" >> $O/final_$name.txt ); tail -4 $O/final_$name.txt | grep -E "passed|failed|[0-9.]+s$" > $O/sum_$name.txt; grep -E "^(FAILED|ERROR)|XPASS" $O/final_$name.txt >> $O/sum_$name.txt; }
for f in protocol:tests/torture_suite/test_protocol_rfc_torture.py boundary:tests/torture_suite/test_boundary_mutants.py; do
 n=${f%%:*}; p=${f#*:}
 ( run ${n}_sqlite1 uv run pytest $p -p no:cacheprovider -q --no-header; run ${n}_sqlite2 uv run pytest $p -p no:cacheprovider -q --no-header; run ${n}_ci HYPOTHESIS_PROFILE=ci uv run pytest $p -p no:cacheprovider -q --no-header -n 4 ) 
 run ${n}_pg1 TEST_DATABASE_URL=$PG uv run pytest $p -p no:cacheprovider -q --no-header
 run ${n}_pg2 TEST_DATABASE_URL=$PG uv run pytest $p -p no:cacheprovider -q --no-header
done
echo done > $O/final_done
