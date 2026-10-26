# Interlinking Report: offline shadow demonstration

> **SYNTHETIC DEMONSTRATION.** Every page analysed in this report is fictional (the reserved
> `research.example.com` demo site). Nothing here is Ken Research content, and no figure below is
> an SEO outcome: nothing was published, crawled or measured against real search traffic.

Database: `ken_app@ken_offline_demo` · mode: **shadow** · CMS: **mock** · published: **nothing** (deployments 0, publish attempts 0).

## 1. What was analysed

| Provenance | Pages | Meaning |
|---|---|---|
| synthetic_base | 64 | fictional demo corpus (65 records, one duplicate URL merged) |
| synthetic_extension | 11 | 11 fictional pages added for orphan / hub-spoke / protected / no-opportunity cases |
| authorized_import | 0 | real content the operator is authorised to use (none unless imported) |
| unlabelled | 0 | imported without a label: NOT counted as real or as synthetic |

75 pages in total, 72 published. Page types: corporate 4, hub 8, insight 16, report 46, transactional 1.

## 2. Internal-link architecture (existing links, before any recommendation)

Existing links: internal 119, external 4. Orphan pages (no internal link points to them): 12 of 72 published.

**Hubs and what they link to**

| Hub | Outbound links | Inbound links | Provenance |
|---|---|---|---|
| / | 7 | 0 | synthetic_base |
| /industries/automotive | 7 | 1 | synthetic_base |
| /industries/energy | 6 | 1 | synthetic_base |
| /industries/healthcare | 5 | 1 | synthetic_base |
| /industries/logistics | 4 | 2 | synthetic_extension |
| /industries/technology | 6 | 1 | synthetic_base |
| /industry-reports | 37 | 1 | synthetic_base |
| /insights | 13 | 1 | synthetic_base |

**Orphan and weakly linked pages**

| Page | Type | Inbound | Situation | Provenance |
|---|---|---|---|---|
| / | hub | 0 |  | synthetic_base |
| /about | corporate | 0 |  | synthetic_base |
| /careers | corporate | 0 |  | synthetic_base |
| /contact | transactional | 1 |  | synthetic_base |
| /industries/automotive | hub | 1 |  | synthetic_base |
| /industries/energy | hub | 1 |  | synthetic_base |
| /industries/healthcare | hub | 1 |  | synthetic_base |
| /industries/technology | hub | 1 |  | synthetic_base |
| /industry-reports | hub | 1 |  | synthetic_base |
| /industry-reports/egypt-fintech-market | report | 0 | country-specific study with natural unlinked mentions of India digital lending/payments | synthetic_extension |
| /industry-reports/global-postal-philately-market | report | 0 | no valid opportunity: topically unrelated to every other page | synthetic_extension |
| /industry-reports/india-battery-swapping-market | report | 0 | orphan: sector-specific study with strong siblings (two-wheelers, charging, swapping insight) | synthetic_extension |
| /industry-reports/india-cold-chain-market | report | 1 |  | synthetic_base |
| /industry-reports/india-digital-lending-market | report | 1 |  | synthetic_base |
| /industry-reports/india-health-insurance-market | report | 1 |  | synthetic_base |
| /industry-reports/india-lithium-ion-battery-manufacturing-market | report | 1 |  | synthetic_base |
| /industry-reports/india-warehousing-market | report | 1 | spoke: links up to the hub only; sideways opportunities to cold chain and last-mile | synthetic_extension |
| /industry-reports/indonesia-ecommerce-market | report | 1 |  | synthetic_base |
| /industry-reports/kuwait-diagnostics-market | report | 0 | draft: unpublished, never a valid target | synthetic_extension |
| /industry-reports/philippines-cold-chain-market | report | 1 |  | synthetic_base |
| /industry-reports/philippines-ev-charging-market | report | 0 | orphan: country-specific study, related to the India/Indonesia charging reports | synthetic_extension |
| /industry-reports/qatar-insurance-market | report | 1 |  | synthetic_base |
| /industry-reports/southeast-asia-last-mile-delivery-market | report | 1 | spoke: links up to the hub only; sideways opportunities to e-commerce logistics reports | synthetic_extension |
| /industry-reports/uae-cold-chain-market | report | 1 |  | synthetic_base |
| /industry-reports/uae-fintech-regulatory-brief | report | 0 | protected: CMS-protected page; must never be edited or receive a link | synthetic_extension |
| /industry-reports/vietnam-ev-charging-market | report | 0 |  | synthetic_base |
| /insights | hub | 1 |  | synthetic_base |
| /insights/charging-infrastructure-gaps-southeast-asia | insight | 1 |  | synthetic_base |
| /insights/ev-fleet-electrification-india | insight | 0 | orphan insight that already carries three existing links (those must not be re-proposed) | synthetic_extension |
| /insights/last-mile-delivery-economics-thailand-vietnam | insight | 1 |  | synthetic_base |
| /insights/office-furniture-design-trends | insight | 0 | no valid opportunity: unrelated insight | synthetic_extension |
| /insights/perovskite-solar-cells-commercialisation | insight | 1 |  | synthetic_base |
| /insights/q3-market-update | insight | 1 |  | synthetic_base |
| /insights/solid-state-batteries-outlook | insight | 0 |  | synthetic_base |
| /insights/webinar-recap-october | insight | 1 |  | synthetic_base |
| /privacy | corporate | 0 |  | synthetic_base |
| /terms | corporate | 0 |  | synthetic_base |

## 3. Orphan opportunities

12 published orphan pages; 7 proposed links point to 3 of them; 2 would receive a link that the critic accepted (the shadow engine publishes none).

| Orphan target | Proposed from | Anchor | Critic | Shadow outcome |
|---|---|---|---|---|
| /insights/ev-fleet-electrification-india | /insights/battery-swapping-electric-two-wheelers-india | electric cars | accept | auto_publish_with_extra_verification |
| /industry-reports/egypt-fintech-market | /insights/digital-payments-lessons-southeast-asia | digital lending | reject | block |
| /industry-reports/india-battery-swapping-market | /insights/battery-swapping-electric-two-wheelers-india | Battery swapping | accept | auto_publish_with_extra_verification |
| /industry-reports/india-battery-swapping-market | /industry-reports/india-ev-charging-infrastructure-market | battery swapping | reject | block |
| /insights/ev-fleet-electrification-india | /insights/charging-infrastructure-gaps-southeast-asia | Electric vehicle | uncertain | none (critic did not accept) |
| /industry-reports/india-battery-swapping-market | /industry-reports/indonesia-ev-charging-infrastructure-market | battery swapping | reject | block |
| /industry-reports/india-battery-swapping-market | /industry-reports/philippines-ev-charging-market | battery swapping | reject | block |

## 4. Candidate generation and funnel

| Outcome of a candidate pair | Count |
|---|---|
| no_natural_anchor | 1691 |
| (candidate became a plan) | 56 |
| target_ineligible | 52 |
| target_excluded | 37 |
| already_linked | 32 |
| target_protected | 29 |
| below_min_relevance | 22 |
| link_density_exceeded | 7 |
| below_min_score | 3 |
| per_source_limit | 1 |

56 link plans were created. Opportunity signals: link_capacity 46, orphan 7, underlinked 40.

## 5. Recommendations (every plan; the CSV carries all columns)

| # | Source | Destination | Anchor | Placement context | Score | Why (top components) | Critic | Shadow outcome (would be) | Confidence |
|---|---|---|---|---|---|---|---|---|---|
| 1 | /industry-reports/india-electric-vehicle-market | /industry-reports/india-ev-charging-infrastructure-market | charging infrastructure | …nd resale value as the main barriers. Public  [charging infrastructure]  along intercity highways is expanding, but h… | 0.74 | semantic 0.17, taxonomy 0.16, intent 0.14, graph 0.15, geography 0.08, commercial 0.05 | accept | auto_publish_with_extra_verification | 0.74 |
| 2 | /industry-reports/india-electric-vehicle-market | /industry-reports/india-auto-components-market | Auto parts | …lly from imported lithium-ion battery cells.  [Auto parts]  suppliers that built their business on engin… | 0.70 | semantic 0.13, taxonomy 0.14, intent 0.14, graph 0.15, geography 0.08, commercial 0.05, underlinked 0.01 | accept | auto_publish_with_extra_verification | 0.70 |
| 3 | /insights/cold-chain-vaccine-distribution-india | /industry-reports/india-cold-chain-market | temperature-controlled logistics | … villages, and it has become a reference for  [temperature-controlled logistics]  across the country.… | 0.70 | semantic 0.12, taxonomy 0.20, intent 0.15, graph 0.15, geography 0.08, commercial 0.05, underlinked 0.03 | accept | auto_publish_with_extra_verification | 0.70 |
| 4 | /industry-reports/gcc-energy-storage-market | /industry-reports/saudi-arabia-solar-power-market | photovoltaic | …n which developers bid a single tariff for a  [photovoltaic]  plant and a co-located battery storage syste… | 0.70 | semantic 0.15, taxonomy 0.12, intent 0.14, graph 0.15, geography 0.08, commercial 0.05, underlinked 0.01 | accept | auto_publish_with_extra_verification | 0.70 |
| 5 | /industry-reports/india-auto-components-market | /industry-reports/india-electric-vehicle-market | electric vehicles | …The shift to  [electric vehicles]  is reshaping demand for auto parts. Supplier… | 0.69 | semantic 0.13, taxonomy 0.14, intent 0.14, graph 0.15, geography 0.08, commercial 0.05 | accept | auto_publish_with_extra_verification | 0.69 |
| 6 | /insights/battery-swapping-electric-two-wheelers-india | /insights/ev-fleet-electrification-india | electric cars | …attery swapping is not new: it was tried for  [electric cars]  more than a decade ago and failed because ca… | 0.67 | semantic 0.09, taxonomy 0.16, intent 0.12, graph 0.15, geography 0.08, commercial 0.03, underlinked 0.04 | accept | auto_publish_with_extra_verification | 0.67 |
| 7 | /insights/charging-infrastructure-gaps-southeast-asia | /industry-reports/thailand-electric-vehicle-market | electric vehicles | …oncern, alongside price. Carmakers that sell  [electric vehicles]  in Thailand, Indonesia and Malaysia now bund… | 0.67 | semantic 0.07, taxonomy 0.16, intent 0.15, graph 0.15, geography 0.08, commercial 0.05, underlinked 0.01 | accept | auto_publish_with_extra_verification | 0.67 |
| 8 | /industry-reports/india-battery-energy-storage-market | /industry-reports/india-lithium-ion-battery-manufacturing-market | lithium-ion battery manufacturing | …istry. Most cells are imported, but domestic  [lithium-ion battery manufacturing]  under the production-linked incentive scheme… | 0.66 | semantic 0.15, taxonomy 0.14, intent 0.14, graph 0.15, geography 0.08, commercial 0.05, underlinked 0.03 | accept | auto_publish_with_extra_verification | 0.66 |
| 9 | /industry-reports/india-cold-chain-market | /industry-reports/india-warehousing-market | Logistics | …rocery and medicine exports expand. ColdPath  [Logistics]  and other organised players will gain share … | 0.65 | semantic 0.05, taxonomy 0.16, intent 0.14, graph 0.15, geography 0.08, commercial 0.05, underlinked 0.03 | accept | auto_publish_with_extra_verification | 0.65 |
| 10 | /insights/perovskite-solar-cells-commercialisation | /industry-reports/vietnam-solar-power-market | rooftop solar | …the first commercial tandem modules to reach  [rooftop solar]  customers before utility-scale projects, whe… | 0.64 | taxonomy 0.20, intent 0.15, graph 0.15, geography 0.08, commercial 0.05, underlinked 0.01 | reject | block | 0.64 |
| 11 | /insights/last-mile-delivery-economics-thailand-vietnam | /industry-reports/vietnam-ecommerce-logistics-market | Last-mile delivery | … [Last-mile delivery]  accounts for roughly half of the cost of shi… | 0.64 | semantic 0.14, taxonomy 0.20, intent 0.15, graph 0.07, geography 0.08, commercial 0.05, underlinked 0.01 | accept | auto_publish_with_extra_verification | 0.64 |
| 12 | /industry-reports/philippines-ev-charging-market | /industry-reports/thailand-electric-vehicle-market | electric vehicles | …National incentives for  [electric vehicles]  and a gradual build-out of rooftop solar at … | 0.64 | semantic 0.08, taxonomy 0.16, intent 0.14, graph 0.15, geography 0.05, commercial 0.05, underlinked 0.01 | reject | block | 0.64 |
| 13 | /industry-reports/india-lithium-ion-battery-manufacturing-market | /industry-reports/india-battery-energy-storage-market | grid storage | … electric vehicles, consumer electronics and  [grid storage] , yet almost all battery cells are still impo… | 0.64 | semantic 0.15, taxonomy 0.14, intent 0.14, graph 0.15, geography 0.08, commercial 0.05 | accept | auto_publish_with_extra_verification | 0.64 |
| 14 | /insights/battery-swapping-electric-two-wheelers-india | /industry-reports/india-ev-charging-infrastructure-market | charging stations | …rge battery fleets and the density of public  [charging stations] , which competes for the same riders.… | 0.63 | semantic 0.08, taxonomy 0.12, intent 0.15, graph 0.15, geography 0.08, commercial 0.05 | accept | auto_publish_with_extra_verification | 0.63 |
| 15 | /industry-reports/thailand-electric-vehicle-market | /insights/charging-infrastructure-gaps-southeast-asia | fast chargers | …rgy company and private operators are adding  [fast chargers]  at fuel stations and shopping malls, but the… | 0.62 | semantic 0.07, taxonomy 0.16, intent 0.10, graph 0.15, geography 0.08, commercial 0.03, underlinked 0.03 | accept | auto_publish_with_extra_verification | 0.62 |
| 16 | /industry-reports/southeast-asia-battery-materials-market | /industry-reports/india-lithium-ion-battery-manufacturing-market | lithium-ion battery | …processing to rise through 2030. Demand from  [lithium-ion battery]  makers in Asia and Europe will determine how… | 0.62 | semantic 0.07, taxonomy 0.14, intent 0.14, graph 0.15, geography 0.05, commercial 0.05, underlinked 0.03 | accept | auto_publish_with_extra_verification | 0.62 |
| 17 | /industry-reports/india-battery-swapping-market | /insights/battery-swapping-electric-two-wheelers-india | electric two-wheeler | …he vehicle, lowering the upfront price of an  [electric two-wheeler] , but they carry the lithium-ion battery risk… | 0.61 | semantic 0.23, intent 0.10, graph 0.15, geography 0.08, commercial 0.03, underlinked 0.01 | accept | auto_publish_with_extra_verification | 0.61 |
| 18 | /industry-reports/indonesia-ev-charging-infrastructure-market | /industry-reports/thailand-electric-vehicle-market | electric vehicles | …commercial question for every operator. With  [electric vehicles]  still a small share of the car parc, many pu… | 0.60 | semantic 0.05, taxonomy 0.16, intent 0.14, graph 0.15, geography 0.05, commercial 0.05, underlinked 0.01 | accept | auto_publish_with_extra_verification | 0.60 |
| 19 | /industry-reports/india-pharmaceutical-market | /industry-reports/india-cold-chain-market | cold chain | …lised logistics providers, and investment in  [cold chain]  capacity for medicines is growing alongside … | 0.60 | semantic 0.04, taxonomy 0.12, intent 0.14, graph 0.15, geography 0.08, commercial 0.05, underlinked 0.03 | accept | auto_publish_with_extra_verification | 0.60 |
| 20 | /insights/semiconductor-supply-chain-shift-malaysia | /industry-reports/india-semiconductor-market | wafer fabrication | …d India offers large subsidies for its first  [wafer fabrication]  plants. Malaysia's advantage is its ecosyste… | 0.60 | semantic 0.03, taxonomy 0.20, intent 0.15, graph 0.07, geography 0.08, commercial 0.05, underlinked 0.01 | accept | auto_publish_with_extra_verification | 0.60 |
| 21 | /insights/digital-payments-lessons-southeast-asia | /industry-reports/egypt-fintech-market | digital lending | …ch firms such as Zorvex Pay monetise through  [digital lending] , insurance distribution and merchant service… | 0.59 | taxonomy 0.20, intent 0.15, graph 0.07, geography 0.08, commercial 0.05, underlinked 0.04 | reject | block | 0.59 |
| 22 | /insights/fame-scheme-electric-vehicle-adoption-india | /industry-reports/india-ev-charging-infrastructure-market | charging stations | …and commercial fleets. It also funded public  [charging stations]  in cities and on highways, and tied subsidie… | 0.59 | semantic 0.00, taxonomy 0.16, intent 0.15, graph 0.15, geography 0.08, commercial 0.05 | accept | auto_publish_with_extra_verification | 0.59 |
| 23 | /industry-reports/uae-cold-chain-market | /industry-reports/qatar-logistics-market | Logistics | …ts is expensive and hard to retain. ColdPath  [Logistics]  recently opened an automated cold storage fa… | 0.58 | semantic 0.03, taxonomy 0.16, intent 0.14, graph 0.15, geography 0.05, commercial 0.05, underlinked 0.01 | accept | auto_publish_with_extra_verification | 0.58 |
| 24 | /insights/digital-payments-lessons-southeast-asia | /industry-reports/india-digital-lending-market | digital payments | … Payments Interface processes more real-time  [digital payments]  than any other system in the world. Its desi… | 0.58 | semantic 0.06, taxonomy 0.20, intent 0.15, graph 0.07, geography 0.08, commercial 0.05, underlinked 0.03 | reject | block | 0.58 |
| 25 | /insights/gcc-data-center-power-demand | /industry-reports/saudi-arabia-data-center-market | data centers | … Arabia and the UAE. If even half are built,  [data centers]  will become one of the fastest-growing sourc… | 0.58 | semantic 0.09, taxonomy 0.20, intent 0.15, graph 0.07, geography 0.08, commercial 0.05 | accept | auto_publish_with_extra_verification | 0.58 |
| 26 | /industry-reports/india-ev-charging-infrastructure-market | /industry-reports/india-battery-energy-storage-market | battery storage | …etworks. Operators are pairing chargers with  [battery storage]  and rooftop solar to manage peak demand char… | 0.58 | semantic 0.10, taxonomy 0.06, intent 0.14, graph 0.15, geography 0.08, commercial 0.05 | accept | auto_publish_with_extra_verification | 0.58 |
| 27 | /insights/indonesia-nickel-battery-materials | /industry-reports/india-lithium-ion-battery-manufacturing-market | battery cells | …Indonesia wants to go further, into  [battery cells]  and electric vehicles. Local content rules f… | 0.56 | taxonomy 0.14, intent 0.15, graph 0.15, geography 0.05, commercial 0.05, underlinked 0.03 | accept | auto_publish_with_extra_verification | 0.56 |
| 28 | /industry-reports/india-battery-swapping-market | /industry-reports/india-ev-charging-infrastructure-market | EV charging infrastructure | …Swapping competes with and complements  [EV charging infrastructure] . Where home charging is easy it wins on conv… | 0.56 | semantic 0.15, intent 0.14, graph 0.15, geography 0.08, commercial 0.05 | accept | auto_publish_with_extra_verification | 0.56 |
| 29 | /insights/battery-swapping-electric-two-wheelers-india | /industry-reports/india-battery-swapping-market | Battery swapping | … [Battery swapping]  promises to remove the two biggest objection… | 0.56 | semantic 0.23, intent 0.15, graph 0.15, geography 0.08, commercial 0.05, underlinked 0.04 | accept | auto_publish_with_extra_verification | 0.56 |
| 30 | /industry-reports/thailand-electric-vehicle-market | /industries/automotive | auto industry | …a regional vehicle manufacturing base as the  [auto industry]  shifts away from combustion engines.… | 0.55 | semantic 0.07, taxonomy 0.16, intent 0.04, graph 0.15, geography 0.08, commercial 0.02, underlinked 0.03 | accept | auto_publish_with_extra_verification | 0.55 |
| 31 | /industry-reports/malaysia-data-center-market | /industry-reports/singapore-cloud-computing-market | cloud services | … and multinationals. Enterprise migration to  [cloud services]  and data residency rules for financial data … | 0.55 | semantic 0.00, taxonomy 0.12, intent 0.14, graph 0.15, geography 0.08, commercial 0.05, underlinked 0.01 | reject | block | 0.55 |
| 32 | /industry-reports/india-battery-swapping-market | /industry-reports/india-electric-two-wheeler-market | electric two-wheeler | …segment tied to commercial fleets and to the  [electric two-wheeler]  market, supported by government incentives f… | 0.55 | semantic 0.13, intent 0.14, graph 0.15, geography 0.08, commercial 0.05 | uncertain | not evaluated | 0.55 |
| 33 | /industry-reports/india-ev-charging-infrastructure-market | /industry-reports/india-battery-swapping-market | battery swapping | …tive fleet depots, highway fast chargers and  [battery swapping] . It profiles leading participants, reviews r… | 0.54 | semantic 0.15, intent 0.14, graph 0.15, geography 0.08, commercial 0.05, underlinked 0.04 | reject | block | 0.54 |
| 34 | /industry-reports/vietnam-solar-power-market | /insights/perovskite-solar-cells-commercialisation | perovskite solar cells | …ics even as tariffs decline. Next-generation  [perovskite solar cells]  are still at the pilot stage and are not exp… | 0.54 | taxonomy 0.20, intent 0.10, graph 0.15, geography 0.08, commercial 0.03, underlinked 0.03 | accept | auto_publish_with_extra_verification | 0.54 |
| 35 | /industry-reports/india-electric-two-wheeler-market | /insights/battery-swapping-electric-two-wheelers-india | Battery swapping | …eeps most owners dependent on home charging.  [Battery swapping]  is emerging as an alternative for commercial… | 0.52 | semantic 0.12, taxonomy 0.20, intent 0.10, graph 0.15, geography 0.08, commercial 0.03, underlinked 0.01 | accept | auto_publish_with_extra_verification | 0.52 |
| 36 | /industry-reports/philippines-cold-chain-market | /industry-reports/india-warehousing-market | Logistics | …g multi-temperature warehouses, and ColdPath  [Logistics]  entered through an acquisition near Manila.… | 0.52 | taxonomy 0.16, intent 0.14, graph 0.15, commercial 0.05, underlinked 0.03 | accept | auto_publish_with_extra_verification | 0.52 |
| 37 | /insights/charging-infrastructure-gaps-southeast-asia | /industry-reports/india-electric-vehicle-market | electric vehicle | … ownership alone misses the largest group of  [electric vehicle]  users in these markets.… | 0.51 | taxonomy 0.16, intent 0.15, graph 0.15, commercial 0.05 | accept | auto_publish_with_extra_verification | 0.51 |
| 38 | /industry-reports/thailand-ecommerce-logistics-market | /industry-reports/qatar-logistics-market | logistics | …Marketplaces and third-party  [logistics]  providers are building fulfilment centres wi… | 0.51 | taxonomy 0.16, intent 0.14, graph 0.15, commercial 0.05, underlinked 0.01 | reject | block | 0.51 |
| 39 | /industry-reports/gcc-energy-storage-market | /industry-reports/india-lithium-ion-battery-manufacturing-market | battery manufacturing | … and some GCC investors are taking stakes in  [battery manufacturing]  abroad to secure supply.… | 0.50 | taxonomy 0.14, intent 0.14, graph 0.15, commercial 0.05, underlinked 0.03 | accept | auto_publish_with_extra_verification | 0.50 |
| 40 | /industry-reports/philippines-ev-charging-market | /industry-reports/india-electric-vehicle-market | electric vehicle | …The Philippines is early in its  [electric vehicle]  journey, and charging is the main constraint… | 0.49 | semantic 0.05, taxonomy 0.16, intent 0.14, graph 0.15, commercial 0.05 | accept | block | 0.49 |
| 41 | /insights/cold-chain-vaccine-distribution-india | /industry-reports/bangladesh-pharmaceutical-market | pharmaceutical | …y commercial cold chain operators that serve  [pharmaceutical]  companies.… | 0.49 | taxonomy 0.12, intent 0.15, graph 0.07, geography 0.08, commercial 0.05, underlinked 0.01 | accept | block | 0.49 |
| 42 | /industry-reports/vietnam-ecommerce-logistics-market | /industry-reports/india-warehousing-market | logistics | …ing. Express delivery operators, marketplace  [logistics]  arms and the national postal service compete… | 0.49 | semantic 0.02, taxonomy 0.16, intent 0.14, graph 0.15, commercial 0.05, underlinked 0.03 | reject | block | 0.49 |
| 43 | /insights/charging-infrastructure-gaps-southeast-asia | /insights/ev-fleet-electrification-india | Electric vehicle | … [Electric vehicle]  sales in Southeast Asia have grown far faste… | 0.48 | semantic 0.02, taxonomy 0.16, intent 0.12, graph 0.15, commercial 0.03, underlinked 0.04 | uncertain | not evaluated | 0.48 |
| 44 | /industry-reports/thailand-ecommerce-logistics-market | /industry-reports/india-warehousing-market | logistics | …Online marketplaces have transformed parcel  [logistics]  in Thailand. Daily parcel volumes have multi… | 0.47 | semantic 0.00, taxonomy 0.16, intent 0.14, graph 0.15, commercial 0.05, underlinked 0.03 | reject | block | 0.47 |
| 45 | /industry-reports/uae-cold-chain-market | /industry-reports/bangladesh-pharmaceutical-market | pharmaceutical | …zones host regional distribution centres for  [pharmaceutical]  companies, and Good Distribution Practice ce… | 0.47 | taxonomy 0.12, intent 0.14, graph 0.15, commercial 0.05, underlinked 0.01 | accept | block | 0.47 |
| 46 | /industry-reports/uae-data-center-market | /industry-reports/singapore-cloud-computing-market | cloud services | …ey modernise their IT estates and migrate to  [cloud services] .… | 0.47 | taxonomy 0.12, intent 0.14, graph 0.15, commercial 0.05, underlinked 0.01 | accept | block | 0.47 |
| 47 | /industry-reports/india-electric-two-wheeler-market | /industry-reports/india-lithium-ion-battery-manufacturing-market | lithium-ion battery | …ural buyers. Most manufacturers still import  [lithium-ion battery]  cells, which leaves margins exposed to curre… | 0.46 | semantic 0.02, intent 0.14, graph 0.15, geography 0.08, commercial 0.05, underlinked 0.03 | accept | block | 0.46 |
| 48 | /insights/gcc-data-center-power-demand | /industry-reports/gcc-energy-storage-market | energy storage | …solar power plants, increasingly paired with  [energy storage]  to cover night-time load. Cooling adds to de… | 0.46 | semantic 0.03, intent 0.15, graph 0.15, geography 0.08, commercial 0.05 | reject | block | 0.46 |
| 49 | /industry-reports/vietnam-wind-power-market | /industry-reports/gcc-energy-storage-market | battery storage | …wer the system can absorb. Grid planners see  [battery storage]  and new interconnectors as essential to inte… | 0.46 | taxonomy 0.12, intent 0.14, graph 0.15, commercial 0.05 | accept | block | 0.46 |
| 50 | /industry-reports/vietnam-solar-power-market | /industry-reports/gcc-energy-storage-market | energy storage | …industrial buyers. Pairing solar plants with  [energy storage]  is one option grid planners are studying to … | 0.46 | taxonomy 0.12, intent 0.14, graph 0.15, commercial 0.05 | accept | block | 0.46 |
| 51 | /industry-reports/india-lithium-ion-battery-manufacturing-market | /industry-reports/india-electric-two-wheeler-market | electric two-wheelers | …ustry, with hundreds of assemblers supplying  [electric two-wheelers]  and three-wheelers. Safety incidents prompte… | 0.44 | semantic 0.02, intent 0.14, graph 0.15, geography 0.08, commercial 0.05 | accept | block | 0.44 |
| 52 | /industry-reports/india-cold-chain-market | /industry-reports/india-rooftop-solar-market | solar panels | …perating cost, and many operators are adding  [solar panels]  to their roofs.… | 0.43 | intent 0.14, graph 0.15, geography 0.08, commercial 0.05, underlinked 0.01 | accept | block | 0.43 |
| 53 | /industry-reports/indonesia-ev-charging-infrastructure-market | /industry-reports/southeast-asia-battery-materials-market | battery materials | …g investments are positioning Indonesia as a  [battery materials]  hub, and policymakers see the charging netwo… | 0.42 | intent 0.14, graph 0.15, geography 0.08, commercial 0.05 | reject | block | 0.42 |
| 54 | /industry-reports/indonesia-ev-charging-infrastructure-market | /industry-reports/india-battery-swapping-market | battery swapping | …rging stations, AC and DC fast chargers, and  [battery swapping]  networks. It profiles leading participants, … | 0.39 | semantic 0.06, intent 0.14, graph 0.15, commercial 0.05, underlinked 0.04 | reject | block | 0.39 |
| 55 | /industry-reports/malaysia-data-center-market | /insights/gcc-data-center-power-demand | power demand | …Data center  [power demand]  is now a significant share of the national g… | 0.38 | semantic 0.01, taxonomy 0.20, intent 0.10, graph 0.15, commercial 0.03, underlinked 0.01 | reject | block | 0.38 |
| 56 | /industry-reports/philippines-ev-charging-market | /industry-reports/india-battery-swapping-market | battery swapping | …led by fleet depots and mall locations, with  [battery swapping]  a niche complement for two-wheelers rather t… | 0.38 | semantic 0.04, intent 0.14, graph 0.15, commercial 0.05, underlinked 0.04 | reject | block | 0.38 |

## 6. Blocked or declined recommendations

| Source → destination | Anchor | Critic | Critic explanation | Hard blocks | Shadow outcome |
|---|---|---|---|---|---|
| /insights/perovskite-solar-cells-commercialisation → /industry-reports/vietnam-solar-power-market | rooftop solar | reject | Anchor 'rooftop solar' misleads about destination focusing on Vietnam. Context mismatch reduces relevance. | none | block |
| /industry-reports/philippines-ev-charging-market → /industry-reports/thailand-electric-vehicle-market | electric vehicles | reject | Anchor 'electric vehicles' misleads about the destination focusing on Thailand, while the context discusses Philippines. | none | block |
| /insights/digital-payments-lessons-southeast-asia → /industry-reports/egypt-fintech-market | digital lending | reject | Anchor 'digital lending' misleads about the destination, which is about Egypt's fintech market, not lending. | none | block |
| /insights/digital-payments-lessons-southeast-asia → /industry-reports/india-digital-lending-market | digital payments | reject | Anchor 'digital payments' misleads about the destination, which is about India's digital lending market, not payments. | none | block |
| /industry-reports/malaysia-data-center-market → /industry-reports/singapore-cloud-computing-market | cloud services | reject | The anchor 'cloud services' misleads about the destination focusing on Singapore, while the context is about Malaysia. | none | block |
| /industry-reports/india-battery-swapping-market → /industry-reports/india-electric-two-wheeler-market | electric two-wheeler | uncertain | Deterministic guard: a better-ranked candidate uses the same anchor for another page. | none | not evaluated (critic did not accept) |
| /industry-reports/india-ev-charging-infrastructure-market → /industry-reports/india-battery-swapping-market | battery swapping | reject | Anchor 'battery swapping' is misleading in context and weakly relevant to the discussion on charging infrastructure. It  | none | block |
| /industry-reports/thailand-ecommerce-logistics-market → /industry-reports/qatar-logistics-market | logistics | reject | Anchor 'logistics' misleads about destination focusing on Qatar, not Thailand. | none | block |
| /industry-reports/philippines-ev-charging-market → /industry-reports/india-electric-vehicle-market | electric vehicle | accept | Anchor 'electric vehicle' naturally links to the India report, which is relevant to the Philippines context. | none | block |
| /insights/cold-chain-vaccine-distribution-india → /industry-reports/bangladesh-pharmaceutical-market | pharmaceutical | accept | The anchor 'pharmaceutical' is natural and relevant to the context of cold chain operations. The destination fits well w | none | block |
| /industry-reports/vietnam-ecommerce-logistics-market → /industry-reports/india-warehousing-market | logistics | reject | Anchor 'logistics' misleads about destination on India warehousing. Context mismatch between Vietnam logistics and India | none | block |
| /insights/charging-infrastructure-gaps-southeast-asia → /insights/ev-fleet-electrification-india | Electric vehicle | uncertain | Deterministic guard: a better-ranked candidate uses the same anchor for another page. | none | not evaluated (critic did not accept) |
| /industry-reports/thailand-ecommerce-logistics-market → /industry-reports/india-warehousing-market | logistics | reject | Anchor 'logistics' misleads about destination focusing on India, not Thailand. | none | block |
| /industry-reports/uae-cold-chain-market → /industry-reports/bangladesh-pharmaceutical-market | pharmaceutical | accept | The anchor 'pharmaceutical' is natural and relevant to the context of temperature-controlled logistics. The destination  | none | block |
| /industry-reports/uae-data-center-market → /industry-reports/singapore-cloud-computing-market | cloud services | accept | Anchor 'cloud services' naturally fits the context of data centers and migration, and the destination is relevant to the | none | block |
| /industry-reports/india-electric-two-wheeler-market → /industry-reports/india-lithium-ion-battery-manufacturing-market | lithium-ion battery | accept | Anchor 'lithium-ion battery' is natural and relevant to the context of manufacturing and import challenges. The destinat | none | block |
| /insights/gcc-data-center-power-demand → /industry-reports/gcc-energy-storage-market | energy storage | reject | Anchor 'energy storage' is misleading as it diverges from the main topic of data centers and their power demand impact. | none | block |
| /industry-reports/vietnam-wind-power-market → /industry-reports/gcc-energy-storage-market | battery storage | accept | Anchor 'battery storage' naturally fits the context of grid constraints and renewable integration. The destination is re | none | block |
| /industry-reports/vietnam-solar-power-market → /industry-reports/gcc-energy-storage-market | energy storage | accept | Anchor is natural, destination is relevant to the context, and aligns with the reader's journey. | none | block |
| /industry-reports/india-lithium-ion-battery-manufacturing-market → /industry-reports/india-electric-two-wheeler-market | electric two-wheelers | accept | Anchor 'electric two-wheelers' is natural and relevant to the context of battery assembly. The destination fits the topi | none | block |
| /industry-reports/india-cold-chain-market → /industry-reports/india-rooftop-solar-market | solar panels | accept | The anchor 'solar panels' is natural and relevant to the context of electricity costs. The destination is appropriate an | none | block |
| /industry-reports/indonesia-ev-charging-infrastructure-market → /industry-reports/southeast-asia-battery-materials-market | battery materials | reject | The anchor 'battery materials' is less relevant to the context of charging infrastructure and may mislead the reader abo | none | block |
| /industry-reports/indonesia-ev-charging-infrastructure-market → /industry-reports/india-battery-swapping-market | battery swapping | reject | The anchor 'battery swapping' is misleading as it does not align with the context of charging infrastructure and may con | none | block |
| /industry-reports/malaysia-data-center-market → /insights/gcc-data-center-power-demand | power demand | reject | The anchor 'power demand' is relevant to data centers, but the context is about Malaysia, while the destination is about | none | block |
| /industry-reports/philippines-ev-charging-market → /industry-reports/india-battery-swapping-market | battery swapping | reject | Anchor 'battery swapping' is less relevant to the Philippines EV charging context and misleads about the destination's f | none | block |

Pairs the engine refused before they became plans are counted in section 4 (`target_protected`, `target_ineligible`, `target_excluded`, `already_linked`, `no_natural_anchor`, `below_min_relevance`).

## 7. Pages with no valid linking opportunity

| Page | Type | Why (situation label / state) | Provenance |
|---|---|---|---|
| /industry-reports/global-postal-philately-market | report | no valid opportunity: topically unrelated to every other page | synthetic_extension |
| /industry-reports/india-diagnostics-market | report | no sufficiently relevant, unlinked mention | synthetic_base |
| /industry-reports/india-health-insurance-market | report | no sufficiently relevant, unlinked mention | synthetic_base |
| /industry-reports/indonesia-ecommerce-market | report | no sufficiently relevant, unlinked mention | synthetic_base |
| /industry-reports/indonesia-electric-two-wheeler-market | report | no sufficiently relevant, unlinked mention | synthetic_base |
| /industry-reports/malaysia-semiconductor-market | report | no sufficiently relevant, unlinked mention | synthetic_base |
| /industry-reports/philippines-fintech-market | report | no sufficiently relevant, unlinked mention | synthetic_base |
| /industry-reports/qatar-insurance-market | report | no sufficiently relevant, unlinked mention | synthetic_base |
| /industry-reports/saudi-arabia-medical-devices-market | report | protected | synthetic_base |
| /industry-reports/southeast-asia-last-mile-delivery-market | report | spoke: links up to the hub only; sideways opportunities to e-commerce logistics reports | synthetic_extension |
| /industry-reports/uae-diagnostics-market | report | no sufficiently relevant, unlinked mention | synthetic_base |
| /industry-reports/uae-fintech-regulatory-brief | report | protected: CMS-protected page; must never be edited or receive a link | synthetic_extension |
| /insights/diagnostics-chains-consolidation-india | insight | no sufficiently relevant, unlinked mention | synthetic_base |
| /insights/office-furniture-design-trends | insight | no valid opportunity: unrelated insight | synthetic_extension |
| /insights/q3-market-update | insight | no sufficiently relevant, unlinked mention | synthetic_base |
| /insights/webinar-recap-october | insight | no sufficiently relevant, unlinked mention | synthetic_base |

**Designed situations (synthetic extension) versus what the engine did**

| Extension page | Designed situation | Plans out | Plans in | Observed |
|---|---|---|---|---|
| /industries/logistics | hub: links down to its spokes; no page links up to it except its spokes | 0 | 0 | no plan, as designed |
| /industry-reports/egypt-fintech-market | country-specific study with natural unlinked mentions of India digital lending/payments | 0 | 1 | engine proposed links |
| /industry-reports/global-postal-philately-market | no valid opportunity: topically unrelated to every other page | 0 | 0 | no plan, as designed |
| /industry-reports/india-battery-swapping-market | orphan: sector-specific study with strong siblings (two-wheelers, charging, swapping insight) | 3 | 4 | engine proposed links |
| /industry-reports/india-warehousing-market | spoke: links up to the hub only; sideways opportunities to cold chain and last-mile | 0 | 4 | engine proposed links |
| /industry-reports/kuwait-diagnostics-market | draft: unpublished, never a valid target | 0 | 0 | protected/draft: no plan targets it |
| /industry-reports/philippines-ev-charging-market | orphan: country-specific study, related to the India/Indonesia charging reports | 3 | 0 | engine proposed links |
| /industry-reports/southeast-asia-last-mile-delivery-market | spoke: links up to the hub only; sideways opportunities to e-commerce logistics reports | 0 | 0 | no plan found (expected opportunity NOT produced) |
| /industry-reports/uae-fintech-regulatory-brief | protected: CMS-protected page; must never be edited or receive a link | 0 | 0 | protected/draft: no plan targets it |
| /insights/ev-fleet-electrification-india | orphan insight that already carries three existing links (those must not be re-proposed) | 0 | 2 | engine proposed links |
| /insights/office-furniture-design-trends | no valid opportunity: unrelated insight | 0 | 0 | no plan, as designed |

## 8. Evaluation (SYNTHETIC gold judgments, base corpus only)

The 65-page base corpus has hand-written GOOD / ACCEPTABLE / BAD target judgments that were written before the engine existed. They measure whether this software behaves as designed on fictional pages; they are **not** an estimate of SEO or of Ken Research page quality.

| Metric | Value |
|---|---|
| Plans on judged base sources | 50 |
| Label mix | {"good": 21, "unjudged": 10, "acceptable": 19} |
| Precision (GOOD or ACCEPTABLE) | 80% (40/50) |
| BAD-target rate (must be 0) | 0% (0/50) |
| BAD plans the critic accepted (must be 0) | 0 |
| Critic verdict by gold label | {"acceptable/accept": 14, "acceptable/reject": 5, "good/accept": 19, "good/reject": 2, "unjudged/accept": 4, "unjudged/reject": 5, "unjudged/uncertain": 1} |

## 9. Shadow-mode safety

| Check | Result |
|---|---|
| Deployments / deployment items / publish attempts | {"deployments": 0, "deployment_items": 0, "publish_attempts": 0} |
| Autonomy decisions recorded (mode, would-be decision, count) | {"shadow/block": 23, "shadow/auto_publish_with_extra_verification": 31} |
| Every recorded decision is `shadow` | True |

## 10. Findings from authorised real content

**None.** No real or authorised content was imported into this run, so there are no real findings. Import a folder with `import-html-dir ... --provenance authorized_import` or a JSONL export (see `docs/pilot/OFFLINE_INTERLINKING_DEMO.md`).

## 11. What this does not show

- No ranking, traffic, click, crawl-budget or revenue effect: nothing was published or measured.
- Critic and ranking quality on real Ken Research pages is unmeasured; the corpus is short, templated and fictional, and the thresholds are uncalibrated (no review history exists).
- See the evidence list in `docs/pilot/MANAGEMENT_DEMO_GUIDE.md`.
