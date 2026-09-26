function Assert-ReleaseVersion([string]$Manifest,[string]$Version) {
    $package=[regex]::Match($Manifest,'(?ms)^\[package\][^\r\n]*\r?\n(?<body>.*?)(?=^\[|\z)')
    $pattern='(?m)^[ \t]*version[ \t]*=[ \t]*"'+[regex]::Escape($Version)+'"[ \t]*\r?$'
    if(!$package.Success -or $package.Groups['body'].Value -notmatch $pattern){
        throw 'Release version must match Cargo.toml [package].version'
    }
}
