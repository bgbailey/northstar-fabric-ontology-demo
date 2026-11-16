using System.Security.Cryptography;
using System.Text.Json;
using System.Text.RegularExpressions;
using Microsoft.AnalysisServices.Tabular;
using Microsoft.AnalysisServices.Tabular.Tmdl;

if (args.Length != 2)
{
    Console.Error.WriteLine("Usage: TmdlCheck <local-model-root> <source-catalog.json>");
    return 2;
}

var root = Path.GetFullPath(args[0]);
var catalogBytes = File.ReadAllBytes(args[1]);
using var catalog = JsonDocument.Parse(catalogBytes);
var models = catalog.RootElement.GetProperty("models").EnumerateArray().ToArray();
var tables = catalog.RootElement.GetProperty("tables").EnumerateArray()
    .ToDictionary(item => item.GetProperty("id").GetString()!);
var metrics = catalog.RootElement.GetProperty("metrics").EnumerateArray().ToArray();
if (models.Any(model => !Regex.IsMatch(model.GetProperty("id").GetString()!, "^[a-z][a-z0-9_]*$")))
{
    Console.Error.WriteLine("Model aliases must be local source identifiers, not paths.");
    return 2;
}
var folders = models.Select(model =>
    Path.Combine(root, model.GetProperty("id").GetString()! + ".SemanticModel")).ToArray();
var before = Fingerprints(folders, root);
var outcomes = new List<object>();
var failed = 0;
var totalMeasures = 0;

foreach (var source in models)
{
    var alias = source.GetProperty("id").GetString()!;
    var folder = Path.Combine(root, alias + ".SemanticModel");
    var sourceMetrics = metrics.Where(metric => metric.GetProperty("model").GetString() == alias).ToArray();
    try
    {
        var database = TmdlSerializer.DeserializeDatabaseFromFolder(Path.Combine(folder, "definition"));
        var errors = new List<string>();
        if (database.CompatibilityLevel != 1702)
            errors.Add("Semantic-model compatibility level must remain 1702.");
        using var properties = JsonDocument.Parse(File.ReadAllText(Path.Combine(folder, "definition.pbism")));
        if (properties.RootElement.GetProperty("version").GetString() != "4.2"
            || !properties.RootElement.GetProperty("settings").GetProperty("qnaEnabled").GetBoolean())
            errors.Add("Reviewed definition.pbism version 4.2/Q&A setting changed.");

        var expectedTables = source.GetProperty("tables").EnumerateArray()
            .Select(id => tables[id.GetString()!]).ToArray();
        if (!database.Model.Tables.Select(table => table.Name).ToHashSet()
            .SetEquals(expectedTables.Select(table => table.GetProperty("displayName").GetString()!)))
            errors.Add("Parsed table membership differs from the source catalog.");
        foreach (var expected in expectedTables)
        {
            var name = expected.GetProperty("displayName").GetString()!;
            var table = database.Model.Tables.FirstOrDefault(table => table.Name == name);
            if (table is null)
                continue;
            var columns = expected.GetProperty("columns").EnumerateObject().ToArray();
            if (!table.Columns.Select(column => column.Name).ToHashSet()
                .SetEquals(columns.Select(column => column.Name)))
                errors.Add($"Column membership differs: {name}.");
            foreach (var expectedColumn in columns)
            {
                var column = table.Columns.FirstOrDefault(column => column.Name == expectedColumn.Name);
                var expectedType = expectedColumn.Value.GetString() switch
                {
                    "int64" => DataType.Int64,
                    "string" => DataType.String,
                    "bool" => DataType.Boolean,
                    "date" or "timestamp" => DataType.DateTime,
                    _ => throw new ArgumentException("Unsupported source contract column type.")
                };
                if (column is not DataColumn dataColumn || dataColumn.DataType != expectedType
                    || dataColumn.SourceColumn != expectedColumn.Name)
                    errors.Add($"Column type/source identity differs: {name}.{expectedColumn.Name}.");
            }
        }

        var actualMeasures = database.Model.Tables.SelectMany(table => table.Measures).ToArray();
        if (actualMeasures.Length != sourceMetrics.Length)
            errors.Add("Parsed source measure count differs from the catalog.");
        foreach (var metric in sourceMetrics)
        {
            var id = metric.GetProperty("id").GetString()!;
            var home = tables[metric.GetProperty("table").GetString()!].GetProperty("displayName").GetString()!;
            var name = metric.GetProperty("name").GetString()!;
            var measure = database.Model.Tables.FirstOrDefault(table => table.Name == home)?
                .Measures.FirstOrDefault(measure => measure.Name == name);
            if (measure is null || measure.Expression.Trim() != metric.GetProperty("dax").GetString()!.Trim()
                || string.IsNullOrWhiteSpace(measure.FormatString)
                || measure.Annotations.FirstOrDefault(annotation => annotation.Name == "MetricId")?.Value != id
                || measure.Annotations.FirstOrDefault(annotation => annotation.Name == "Aggregation")?.Value
                    != metric.GetProperty("aggregation").GetString())
                errors.Add($"Measure/home/expression/format/identity/aggregation differs: {id}.");
        }
        if (errors.Count != 0)
            failed++;
        totalMeasures += actualMeasures.Length;
        outcomes.Add(new
        {
            model = alias,
            status = errors.Count == 0 ? "LOCAL_TOM_METADATA_VALIDATED" : "FAILED",
            tables = database.Model.Tables.Count,
            measures = actualMeasures.Length,
            errors
        });
    }
    catch (TmdlFormatException error)
    {
        failed++;
        outcomes.Add(new
        {
            model = alias,
            status = "FAILED_TMDL_FORMAT",
            document = error.Document,
            line = error.Line,
            error = error.Message
        });
    }
    catch (TmdlSerializationException error)
    {
        failed++;
        outcomes.Add(new { model = alias, status = "FAILED_TMDL_METADATA", error = error.Message });
    }
}

var after = Fingerprints(folders, root);
var catalogUnchanged = File.ReadAllBytes(args[1]).SequenceEqual(catalogBytes);
var unchanged = catalogUnchanged && before.Count == after.Count
    && before.All(pair => after.GetValueOrDefault(pair.Key) == pair.Value);
if (!unchanged)
    failed++;
Console.WriteLine(System.Text.Json.JsonSerializer.Serialize(new
{
    status = failed == 0 ? "LOCAL_TOM_METADATA_VALIDATED_NOT_LIVE_DAX" : "FAILED",
    observed_at = DateTimeOffset.UtcNow,
    parser_assembly = typeof(TmdlSerializer).Assembly.GetName().Name,
    parser_version = typeof(TmdlSerializer).Assembly.GetName().Version?.ToString(),
    models = outcomes,
    measures = totalMeasures,
    source_files_unchanged = unchanged,
    source_catalog_unchanged = catalogUnchanged,
    source_files_sha256 = before,
    source_catalog_sha256 = Convert.ToHexString(SHA256.HashData(catalogBytes)).ToLowerInvariant(),
    live_dax = "NOT_RUN",
    deployed = false,
    server_connections = 0,
    limitations = new[]
    {
        "TMDL/TOM metadata deserialization and source-contract matching only; not DAX engine execution.",
        "Physical local model definitions are not an exhaustive AI-visible or deployed source schema.",
        "No source refresh, cloud mutation, authentication, query or source RLS/OLS proof."
    }
}, new JsonSerializerOptions { WriteIndented = true }));
return failed == 0 ? 0 : 1;

static Dictionary<string, string> Fingerprints(IEnumerable<string> folders, string root)
{
    return folders.SelectMany(folder => Directory.EnumerateFiles(folder, "*", SearchOption.AllDirectories))
        .OrderBy(path => path, StringComparer.Ordinal)
        .ToDictionary(path => Path.GetRelativePath(root, path),
            path => Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(path))).ToLowerInvariant());
}
