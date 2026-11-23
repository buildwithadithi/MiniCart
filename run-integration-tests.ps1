
$tests = @(
    @{
        Name = "Product Service"
        Command = "product-service\tests"
        Python = ".\product-service\.venv\Scripts\python.exe"
    },
    @{
        Name = "Order Service"
        Command = "order-service\tests"
        Python = ".\order-service\.venv\Scripts\python.exe"
    },
    @{
        Name = "End-to-End"
        Command = "tests\e2e"
        Python = ".\.venv\Scripts\python.exe"
    },
    @{
        Name = "Gateway Integration"
        Command = "tests\gateway"
        Python = ".\.venv\Scripts\python.exe"
    }
)

foreach ($test in $tests) {
    Write-Host "`n========== $($test.Name) ==========" -ForegroundColor Cyan

    & $test.Python -m pytest $test.Command -v

    if ($LASTEXITCODE -ne 0) {
        Write-Host "$($test.Name) FAILED. Stopping." -ForegroundColor Red
        exit 1
    }
}

Write-Host "`nAll integration suites passed!" -ForegroundColor Green
