| **Version** | **Date Modified (DD-MM-YYYY)** | **Change History**                                                 |
|-------------|--------------------------------|--------------------------------------------------------------------|
| 3.3.0       | 08-10-2026                     | Added **Assets** data type to the Function App data connector: new `AssetsDetails` function, `BitsightAssets_data_CL` table, `Schedule_Assets` and `Assets_Refresh_Hours` settings, and new parser **BitSightAssets**. Only new, updated and removed assets are ingested (`change_type` column). |
| 3.2.0       | 15-06-2026                     | Replaced legacy Function App connector with two **Codeless Connector Framework (CCF)** connectors: **BitSight Security Events** (Alerts, Breaches, Findings) and **BitSight Security Statistics** (CompanyDetails, CompanyRatingDetails, DiligenceHistoricalStatistics, DiligenceStatistics, ObservationStatistics, IndustrialStatistics, VulnerabilitiesFindingsSummary, FindingsSummary). Added parsers for **BitSightCompanyRatingDetails** and **BitSightVulnerabilitiesFindingsSummary**. Updated Function App UI page for Log Ingestion API. |
| 3.1.1       | 22-04-2026                     | Updated **Solution Package** with the fix of solutionId |
| 3.1.0       | 31-03-2026                     | Updated the python runtime version to 3.12. Added support for Log Ingestion API and updated parsers accordingly. <br> Reverted the solution id to fix the BitSight Solution publishing issue.                   |
| 3.0.2       | 26-07-2024                     | Update **Analytic rules** for missing TTP                          |
| 3.0.1       | 15-04-2024                     | Added Bitsight prefix in data tables name                           |
| 3.0.0       | 23-01-2024                     | Updated **Data Connector** code with the fix of Pagination and Checkpoint related issue |
         
                                                                                                                 
